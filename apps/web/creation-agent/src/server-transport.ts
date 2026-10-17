import { z } from 'zod'
import type { Message, StreamEvent, Transport } from './model.js'
const stepSchema = z
  .object({
    id: z.string(),
    title: z.string().optional(),
    text: z.string().optional(),
    detail: z.string().optional(),
    status: z.enum(['pending', 'in_progress', 'completed', 'failed', 'cancelled', 'skipped']),
    dependsOn: z.array(z.string()).optional(),
    parallelGroup: z.string().optional(),
  })
  .refine((step) => Boolean(step.title?.trim() || step.text?.trim()), '计划步骤缺少文案')
const confirmSchema = z.object({
  kind: z.literal('confirm'),
  confirmId: z.string(),
  interrupt_kind: z.enum(['credit', 'clarify']).optional(),
  title: z.string(),
  estimateCredits: z.number(),
  summary: z.string(),
  state: z.enum(['pending', 'accepted', 'rejected', 'expired']),
})
const partSchema = z.discriminatedUnion('kind', [
  z.object({ kind: z.literal('text'), text: z.string() }),
  z.object({ kind: z.literal('thinking'), text: z.string(), summary: z.string().optional() }),
  z.object({ kind: z.literal('tool_call'), summary: z.string(), detail: z.string() }),
  z.object({ kind: z.literal('tool_result'), summary: z.string(), detail: z.string() }),
  z.object({
    kind: z.literal('tool_status'),
    callId: z.string(),
    toolName: z.string(),
    label: z.string(),
    state: z.enum(['loading', 'completed', 'failed', 'cancelled']),
  }),
  z.object({
    kind: z.literal('artifact'),
    id: z.string(),
    type: z.enum(['image', 'video']),
    url: z.string(),
    status: z.enum(['success', 'failed']),
    qualityIssue: z.string().optional(),
    tier: z.enum(['preview', 'final']).optional(),
    createdByStepId: z.string().optional(),
  }),
  z.object({
    kind: z.literal('plan'),
    explanation: z.string().optional(),
    steps: z.array(stepSchema),
  }),
  confirmSchema,
  z.object({ kind: z.literal('error'), message: z.string(), code: z.string().optional() }),
])
const messageSchema = z.object({
  id: z.string(),
  role: z.literal('assistant'),
  status: z.enum(['streaming', 'completed', 'failed', 'cancelled']),
  parts: z.array(partSchema),
})
const systemSchema = z.discriminatedUnion('type', [
  z.object({ kind: z.literal('system'), type: z.literal('stream_start') }),
  z.object({
    kind: z.literal('system'),
    type: z.literal('stream_complete'),
    status: z.enum(['running', 'interrupted', 'completed', 'failed', 'cancelled']).optional(),
  }),
  z.object({
    kind: z.literal('system'),
    type: z.literal('stream_error'),
    message: z.string().optional(),
  }),
  z.object({
    kind: z.literal('system'),
    type: z.literal('todo_plan'),
    explanation: z.string().optional(),
    steps: z.array(stepSchema),
  }),
  z.object({
    kind: z.literal('system'),
    type: z.literal('interrupt_approval'),
    interrupt_kind: z.enum(['credit', 'clarify']).optional(),
    confirmId: z.string(),
    title: z.string(),
    estimateCredits: z.number(),
    summary: z.string(),
  }),
])
const eventSchema = z
  .object({
    conversation_id: z.uuid(),
    turn_uuid: z.uuid(),
    runId: z.uuid(),
    sequence: z.number().int().positive(),
  })
  .and(
    z.union([
      systemSchema,
      z.object({ kind: z.literal('text_delta'), text: z.string() }),
      z.object({ kind: z.literal('message'), message: messageSchema }),
    ]),
  )
export async function* readSse(response: Response, signal: AbortSignal): AsyncGenerator<unknown> {
  if (!response.body) {
    throw new Error('服务端未返回事件流')
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (true) {
      signal.throwIfAborted()
      const { done, value } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      buffer = buffer.replace(/\r\n/g, '\n')
      let separator: number
      while ((separator = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, separator)
        buffer = buffer.slice(separator + 2)
        const data = frame
          .split('\n')
          .filter((line) => line.startsWith('data:'))
          .map((line) => line.slice(5).trimStart())
          .join('\n')
        if (data) {
          yield JSON.parse(data) as unknown
        }
      }
      if (done) {
        break
      }
    }
  } finally {
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}
interface StreamContext {
  emit: (event: StreamEvent) => void
  signal: AbortSignal
  turn?: { conversation_id: string; turn_uuid: string }
  serverRunId?: string
  lastSequence: number
  interrupted: boolean
  started: boolean
  inFlight?: Promise<void>
}
export function createServerTransport(baseUrl = 'http://127.0.0.1:8787'): Transport {
  let stopPending: Promise<void> = Promise.resolve()
  let conversationId: string | undefined
  const contexts = new Map<string, StreamContext>()
  const exchange = async (runId: string, context: StreamContext, body: object) => {
    context.started = false
    let terminal = false
    const response = await fetch(`${baseUrl}/v1/conversation`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: context.signal,
    })
    if (!response.ok) {
      throw new Error(`服务端请求失败（${response.status}）`)
    }
    if (!response.headers.get('content-type')?.includes('text/event-stream')) {
      throw new Error('服务端未返回 SSE')
    }
    context.interrupted = false
    try {
      for await (const raw of readSse(response, context.signal)) {
        const event = eventSchema.parse(raw)
        if (context.serverRunId && context.serverRunId !== event.runId) {
          throw new Error('事件流运行 ID 不一致')
        }
        context.serverRunId = event.runId
        if (event.sequence <= context.lastSequence) {
          continue
        }
        context.lastSequence = event.sequence
        conversationId = event.conversation_id
        context.turn = { conversation_id: event.conversation_id, turn_uuid: event.turn_uuid }
        context.signal.throwIfAborted()
        if (event.kind === 'text_delta') {
          context.emit({ runId, type: 'text_delta', text: event.text })
        } else if (event.kind === 'message') {
          const message: Message = event.message
          context.emit({ runId, type: 'message_snapshot', message })
        } else {
          switch (event.type) {
            case 'stream_start':
              context.started = true
              context.emit({ runId, type: 'stream_start' })
              break
            case 'todo_plan':
              context.emit({
                runId,
                type: 'plan_update',
                explanation: event.explanation,
                steps: event.steps,
              })
              break
            case 'interrupt_approval':
              terminal = true
              context.interrupted = true
              context.emit({
                runId,
                type: 'confirm_required',
                confirm: {
                  kind: 'confirm',
                  confirmId: event.confirmId,
                  interrupt_kind: event.interrupt_kind,
                  title: event.title,
                  estimateCredits: event.estimateCredits,
                  summary: event.summary,
                  state: 'pending',
                },
              })
              break
            case 'stream_error':
              terminal = true
              context.emit({
                runId,
                type: 'stream_error',
                message: event.message ?? '服务端执行失败',
              })
              break
            case 'stream_complete':
              terminal = true
              context.emit({
                runId,
                type: 'stream_complete',
                status: event.status === 'cancelled' ? 'cancelled' : 'completed',
              })
              break
          }
        }
      }
      if (!terminal && !context.signal.aborted) {
        throw new Error('事件流提前结束，请重新发送。')
      }
    } finally {
      if (!context.interrupted || context.signal.aborted) {
        contexts.delete(runId)
      }
    }
  }
  return {
    remoteInterrupts: true,
    async start(runId, _complex, signal, emit, _faults, prompt) {
      if (!prompt?.trim()) {
        throw new Error('创作需求不能为空')
      }
      await stopPending
      signal.throwIfAborted()
      const context: StreamContext = {
        emit,
        signal,
        lastSequence: 0,
        interrupted: false,
        started: false,
      }
      contexts.set(runId, context)
      try {
        const pending = exchange(runId, context, { prompt, conversation_id: conversationId })
        context.inFlight = pending
        await pending
      } catch (error: unknown) {
        contexts.delete(runId)
        throw error
      }
    },
    async resumeInterrupt(runId, confirmId, signal, faults, approved = true) {
      const context = contexts.get(runId)
      if (!context?.turn || !context.interrupted) {
        throw new Error('确认已失效')
      }
      if (faults.resume) {
        throw new Error('继续执行失败，请再次确认。')
      }
      await context.inFlight
      signal.throwIfAborted()
      context.signal = signal
      try {
        const pending = exchange(runId, context, {
          conversation_id: context.turn.conversation_id,
          action: 'resume_interrupt',
          confirmId,
          approved,
        })
        context.inFlight = pending
        await pending
      } catch (error: unknown) {
        if (context.started && !signal.aborted) {
          context.emit({
            runId,
            type: 'stream_error',
            message: error instanceof Error ? error.message : '恢复流中断',
          })
          contexts.delete(runId)
          return
        }
        context.interrupted = true
        throw error
      }
    },
    stop(runId) {
      const context = contexts.get(runId)
      if (context?.turn) {
        stopPending = fetch(`${baseUrl}/v1/stop_stream`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(context.turn),
          keepalive: true,
        })
          .then(() => {})
          .catch(() => {})
      }
      contexts.delete(runId)
    },
  }
}
