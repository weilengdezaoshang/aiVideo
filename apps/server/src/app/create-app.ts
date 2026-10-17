import { CapabilityCatalog, type Capability } from '../agent/capabilities.js'
import type { RoutingOptions } from '../agent/intent.js'
import { createReviewer, type ReviewerConfig } from '../adapters/reviewer/factory.js'
import { validateReflectionOptions, type ReflectionOptions } from '../agent/reflection.js'
import { fileURLToPath } from 'node:url'
import { AgentLoop } from '../agent/host.js'
import { createApi } from '../api/routes.js'
import {
  MemoryAsyncQueue,
  MemoryEventLog,
  MemoryRateLimiter,
  MemorySessionStore,
  MemoryTaskQueue,
  StubLlmProvider,
} from '../adapters/memory/index.js'
import { ConversationService } from '../domain/conversation-service.js'
import type { Ports, Tool } from '../ports/index.js'
import { FileTraceWriter } from '../trace/file-writer.js'
import { createText2Image, placeholderSvg } from '../tools/text2image.js'

export interface AppOptions extends ReflectionOptions, RoutingOptions {
  environment?: 'development' | 'production'
  capabilities?: readonly Capability[]
  reviewer?: ReviewerConfig
  queueOptions?: {
    async?: boolean
    timeoutMs?: number
    maxPending?: number
    retentionMs?: number
    maxRecords?: number
  }
  rateOptions?: { requests?: number; windowMs?: number; concurrency?: number; maxKeys?: number }
  ports?: Partial<Ports>
  tools?: readonly Tool[]
  traceDirectory?: string
  publicBaseUrl?: string
  origins?: string[]
  llmDelayMs?: number
  toolDelayMs?: number
  creditThreshold?: number
  imageCredits?: number
  streamCharDelayMs?: number
}

// 唯一 composition root，api/domain/agent 仅依赖 ports。
export function createApplication(options: AppOptions = {}) {
  if (options.environment === 'production' || process.env.NODE_ENV === 'production') {
    throw new Error('创作 Host 尚未接入持久化生产 ports，禁止以生产模式启动')
  }
  validateReflectionOptions(options)
  if (
    options.maxParallelSteps !== undefined &&
    (!Number.isSafeInteger(options.maxParallelSteps) ||
      options.maxParallelSteps < 1 ||
      options.maxParallelSteps > 32)
  ) {
    throw new Error('maxParallelSteps 必须为 1–32 整数')
  }
  const queue =
    options.ports?.queue ??
    (options.queueOptions?.async
      ? new MemoryAsyncQueue(options.queueOptions)
      : new MemoryTaskQueue(options.queueOptions))
  const tools = options.tools ?? [
    createText2Image(
      options.publicBaseUrl ?? 'http://localhost:8787',
      options.toolDelayMs,
      options.imageCredits,
    ),
  ]

  for (const tool of tools) {
    queue.register(tool.name, async (payload, ctx) => {
      const input =
        payload && typeof payload === 'object' && 'prompt' in (payload as object)
          ? (payload as { prompt: string })
          : { prompt: '' }
      return tool.execute(input, ctx.signal ?? new AbortController().signal)
    })
  }
  const ports: Ports = {
    reviewer: options.ports?.reviewer ?? createReviewer(options.reviewer),
    sessions: options.ports?.sessions ?? new MemorySessionStore(),
    events: options.ports?.events ?? new MemoryEventLog(),
    queue,
    limiter: options.ports?.limiter ?? new MemoryRateLimiter(options.rateOptions),
    llm: options.ports?.llm ?? new StubLlmProvider(options.llmDelayMs),
    traces:
      options.ports?.traces ??
      new FileTraceWriter(
        options.traceDirectory ?? fileURLToPath(new URL('../../../../eval/runs/', import.meta.url)),
      ),
  }

  const service = new ConversationService(
    ports,
    new AgentLoop(
      ports,
      tools,
      options.streamCharDelayMs ?? options.llmDelayMs ?? 35,
      options.creditThreshold ?? 50,
      options,
      options,
      options.capabilities ? new CapabilityCatalog(options.capabilities) : undefined,
    ),
  )
  const api = createApi(
    service,
    options.origins ?? ['http://localhost:5173', 'http://127.0.0.1:5173'],
    placeholderSvg,
  )
  return { api, ports, service }
}
