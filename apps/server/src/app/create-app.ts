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
import { createText2Image, placeholderSvg, toolName } from '../tools/text2image.js'

export interface AppOptions {
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
  // 确保默认生图名已注册（自定义 tools 覆盖时仍可按名 enqueue）
  if (!tools.some((t) => t.name === toolName)) {
    const fallback = createText2Image(
      options.publicBaseUrl ?? 'http://localhost:8787',
      options.toolDelayMs,
    )
    queue.register(fallback.name, async (payload, ctx) =>
      fallback.execute(payload as { prompt: string }, ctx.signal ?? new AbortController().signal),
    )
  }

  const ports: Ports = {
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
    ),
  )
  const api = createApi(
    service,
    options.origins ?? ['http://localhost:5173', 'http://127.0.0.1:5173'],
    placeholderSvg,
  )
  return { api, ports, service }
}
