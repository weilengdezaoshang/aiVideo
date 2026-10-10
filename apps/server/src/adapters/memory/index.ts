import { randomUUID } from 'node:crypto'
import { setTimeout as sleep } from 'node:timers/promises'
import type {
  AgentEvent,
  ConversationRecord,
  ContinuationDraft,
  InterruptSnapshot,
  TaskJob,
  Turn,
  TurnRecord,
} from '../../domain/model.js'
import type {
  EventLog,
  LlmProvider,
  RateLimiter,
  SessionStore,
  TaskHandler,
  TaskQueue,
  TraceWriter,
} from '../../ports/index.js'
import type { AgentTraceV1 } from '../../trace/schema.js'

function now() {
  return new Date().toISOString()
}

export class MemorySessionStore implements SessionStore {
  private conversations = new Map<string, ConversationRecord>()
  private turns = new Map<string, TurnRecord>()
  private runtime = new Map<string, Turn>()
  private controllers = new Map<string, AbortController>()

  async saveContinuationDraft(conversationId: string, draft: ContinuationDraft) {
    const conversation = this.conversations.get(conversationId)
    if (!conversation) {
      throw new Error('会话不存在')
    }
    conversation.continuationDraft = structuredClone(draft)
    conversation.updatedAt = now()
  }
  async getContinuationDraft(conversationId: string) {
    const draft = this.conversations.get(conversationId)?.continuationDraft
    return draft ? structuredClone(draft) : null
  }
  async clearContinuationDraft(conversationId: string) {
    const conversation = this.conversations.get(conversationId)
    if (conversation) {
      delete conversation.continuationDraft
      conversation.updatedAt = now()
    }
  }
  async getConversation(id: string) {
    const c = this.conversations.get(id)
    return c ? structuredClone(c) : null
  }
  async saveConversation(c: ConversationRecord) {
    this.conversations.set(c.id, structuredClone(c))
  }
  async createConversation(init: Partial<ConversationRecord> = {}) {
    const createdAt = now()
    const record: ConversationRecord = {
      id: init.id ?? randomUUID(),
      userId: init.userId,
      title: init.title,
      activeAgent: init.activeAgent ?? 'host',
      createdAt: init.createdAt ?? createdAt,
      updatedAt: init.updatedAt ?? createdAt,
    }
    this.conversations.set(record.id, structuredClone(record))
    return structuredClone(record)
  }

  async getTurn(turnId: string) {
    const t = this.turns.get(turnId)
    return t ? structuredClone(t) : null
  }
  async listTurns(conversationId: string) {
    return [...this.turns.values()]
      .filter((t) => t.conversationId === conversationId)
      .map((t) => structuredClone(t))
      .sort((a, b) => a.createdAt.localeCompare(b.createdAt))
  }
  async saveTurn(t: TurnRecord) {
    const previous = this.turns.get(t.id)
    const next = structuredClone(t)
    if (previous?.status === 'cancelled') {
      next.status = 'cancelled'
    }
    this.turns.set(t.id, next)
    const rt = this.runtime.get(t.id)
    if (rt) {
      rt.status = next.status === 'interrupted' ? 'interrupted' : next.status
      if (next.status === 'cancelled') {
        rt.message.status = 'cancelled'
      }
      this.runtime.set(t.id, structuredClone(rt))
    }
  }

  async getInterruptSnapshot(turnId: string) {
    const t = this.turns.get(turnId)
    return t?.snapshot ? structuredClone(t.snapshot) : null
  }
  async saveInterruptSnapshot(turnId: string, snap: InterruptSnapshot) {
    const t = this.turns.get(turnId)
    if (!t) {
      throw new Error(`turn 不存在: ${turnId}`)
    }
    if (t.status === 'cancelled') {
      throw new Error('轮次已停止，不能再保存中断')
    }
    t.snapshot = structuredClone(snap)
    t.status = 'interrupted'
    t.updatedAt = now()
    this.turns.set(turnId, structuredClone(t))
  }
  async clearInterruptSnapshot(turnId: string) {
    const t = this.turns.get(turnId)
    if (!t) {
      return
    }
    t.snapshot = null
    t.updatedAt = now()
    this.turns.set(turnId, structuredClone(t))
  }

  async beginTurn(prompt: string, conversationId?: string): Promise<Turn> {
    let conversation = conversationId ? await this.getConversation(conversationId) : null
    if (!conversation) {
      conversation = await this.createConversation(conversationId ? { id: conversationId } : {})
    } else {
      conversation.activeAgent = 'host'
      conversation.updatedAt = now()
      await this.saveConversation(conversation)
    }
    const turnId = randomUUID()
    const createdAt = now()
    const turn: Turn = {
      conversationId: conversation.id,
      turnId,
      runId: randomUUID(),
      prompt,
      status: 'running',
      createdAt,
      updatedAt: createdAt,
      message: { id: randomUUID(), role: 'assistant', status: 'streaming', parts: [] },
    }
    const record: TurnRecord = {
      id: turnId,
      conversationId: conversation.id,
      status: 'running',
      snapshot: null,
      plan: null,
      createdAt,
      updatedAt: createdAt,
      runId: turn.runId,
      prompt,
      message: structuredClone(turn.message),
    }
    this.turns.set(turnId, structuredClone(record))
    this.runtime.set(turnId, structuredClone(turn))
    this.controllers.set(turnId, new AbortController())
    return structuredClone(turn)
  }

  async getRuntimeTurn(conversationId: string, turnId: string) {
    const turn = this.runtime.get(turnId)
    return turn?.conversationId === conversationId ? structuredClone(turn) : undefined
  }

  async saveRuntimeTurn(turn: Turn) {
    const previous = this.turns.get(turn.turnId)
    if (previous?.status === 'cancelled') {
      turn.status = 'cancelled'
      turn.message.status = 'cancelled'
    }
    const updatedAt = now()
    turn.updatedAt = updatedAt
    this.runtime.set(turn.turnId, structuredClone(turn))
    const record: TurnRecord = {
      id: turn.turnId,
      conversationId: turn.conversationId,
      status: turn.status,
      snapshot: previous?.snapshot ?? null,
      plan: turn.execution?.plan ?? previous?.plan ?? null,
      createdAt: previous?.createdAt ?? turn.createdAt,
      updatedAt,
      runId: turn.runId,
      prompt: turn.prompt,
      message: structuredClone(turn.message),
      execution: turn.execution ? structuredClone(turn.execution) : previous?.execution,
      resolutions: previous?.resolutions,
    }
    if (previous?.status === 'cancelled') {
      record.status = 'cancelled'
    }
    this.turns.set(turn.turnId, structuredClone(record))
  }

  signal(turnId: string) {
    const controller = this.controllers.get(turnId)
    if (!controller) {
      throw new Error('运行信号不存在')
    }
    return controller.signal
  }

  async cancel(conversationId: string, turnId: string) {
    const turn = this.runtime.get(turnId)
    if (!turn || turn.conversationId !== conversationId) {
      return undefined
    }
    if (turn.status === 'running' || turn.status === 'interrupted') {
      turn.status = 'cancelled'
      turn.message.status = 'cancelled'
      turn.updatedAt = now()
      for (const part of turn.message.parts) {
        if (part.kind === 'confirm' && part.state === 'pending') {
          part.state = 'expired'
        }
        if (part.kind === 'tool_status' && part.state === 'loading') {
          part.state = 'cancelled'
          part.label = '已停止'
        }
        if (part.kind === 'plan') {
          for (const step of part.steps) {
            if (step.status === 'in_progress') {
              step.status = 'cancelled'
            }
          }
        }
      }
      this.runtime.set(turnId, structuredClone(turn))
      const record = this.turns.get(turnId)
      if (record) {
        record.status = 'cancelled'
        record.snapshot = null
        record.message = structuredClone(turn.message)
        record.updatedAt = turn.updatedAt
        this.turns.set(turnId, structuredClone(record))
      }
      this.controllers.get(turnId)?.abort()
    }
    return structuredClone(this.runtime.get(turnId))
  }

  async claimInterrupt(turnId: string, confirmId: string, approved: boolean) {
    const record = this.turns.get(turnId)
    if (!record) {
      return 'conflict' as const
    }
    const existing = record.resolutions?.find((r) => r.confirmId === confirmId)
    if (existing) {
      return existing.approved !== approved ? ('conflict' as const) : existing.state
    }
    if (record.status !== 'interrupted' || record.snapshot?.confirmId !== confirmId) {
      return 'conflict' as const
    }
    record.resolutions ??= []
    record.resolutions.push({ confirmId, approved, state: 'running' })
    record.status = 'running'
    record.updatedAt = now()
    this.turns.set(turnId, structuredClone(record))
    return 'claimed' as const
  }
  async activateTurn(turn: Turn) {
    const controller = new AbortController()
    if (this.turns.get(turn.turnId)?.status === 'cancelled') {
      controller.abort()
    }
    this.controllers.set(turn.turnId, controller)
    await this.saveRuntimeTurn(turn)
  }
  async settleInterrupt(turnId: string, confirmId: string) {
    const record = this.turns.get(turnId)
    const receipt = record?.resolutions?.find((r) => r.confirmId === confirmId)
    if (record && receipt) {
      receipt.state = 'settled'
      this.turns.set(turnId, structuredClone(record))
    }
  }

  release(turnId: string) {
    this.controllers.delete(turnId)
  }
}

export class MemoryEventLog implements EventLog {
  private byConversation = new Map<string, AgentEvent[]>()
  private turnIndex = new Map<string, { conversationId: string; seqs: number[] }>()

  async append(e: Omit<AgentEvent, 'seq' | 'at'> & { at?: string }): Promise<AgentEvent> {
    const list = this.byConversation.get(e.conversationId) ?? []
    const seq = list.length === 0 ? 1 : list[list.length - 1]!.seq + 1
    const entry: AgentEvent = {
      seq,
      conversationId: e.conversationId,
      turnId: e.turnId,
      at: e.at ?? now(),
      event: e.event,
      data: structuredClone(e.data),
    }
    list.push(entry)
    this.byConversation.set(e.conversationId, list)
    const idx = this.turnIndex.get(e.turnId) ?? { conversationId: e.conversationId, seqs: [] }
    idx.seqs.push(seq)
    this.turnIndex.set(e.turnId, idx)
    return structuredClone(entry)
  }

  async readFrom(
    conversationId: string,
    seq: number,
    opts?: { limit?: number; turnId?: string },
  ): Promise<AgentEvent[]> {
    let list = (this.byConversation.get(conversationId) ?? []).filter((e) => e.seq > seq)
    if (opts?.turnId) {
      list = list.filter((e) => e.turnId === opts.turnId)
    }
    if (opts?.limit != null) {
      list = list.slice(0, opts.limit)
    }
    return structuredClone(list)
  }

  async getLastSeq(conversationId: string) {
    const list = this.byConversation.get(conversationId) ?? []
    return list.length === 0 ? 0 : list[list.length - 1]!.seq
  }

  async listByTurn(turnId: string) {
    const idx = this.turnIndex.get(turnId)
    if (!idx) {
      return []
    }
    const all = this.byConversation.get(idx.conversationId) ?? []
    return structuredClone(all.filter((e) => e.turnId === turnId))
  }
}

export class MemoryTaskQueue implements TaskQueue {
  private jobs = new Map<string, TaskJob>()
  private byKey = new Map<string, string>()
  private handlers = new Map<string, TaskHandler>()
  private queue: string[] = []
  /** memory：enqueue 后同步消费 handler */
  private pending = new Map<string, Promise<void>>()
  private active = 0
  private owned = new Set<string>()
  private executing = new Set<string>()
  constructor(
    private options: {
      async?: boolean
      timeoutMs?: number
      maxPending?: number
      retentionMs?: number
      maxRecords?: number
      clock?: () => number
    } = {},
  ) {}
  private prune() {
    const time = this.options.clock?.() ?? Date.now()
    for (const [id, job] of this.jobs) {
      if (
        !this.pending.has(id) &&
        !this.executing.has(id) &&
        ['completed', 'failed', 'cancelled'].includes(job.status) &&
        time - Date.parse(job.updatedAt) >= (this.options.retentionMs ?? 86400000)
      ) {
        this.jobs.delete(id)
        this.byKey.delete(job.idempotencyKey)
      }
    }
  }

  register(name: string, handler: TaskHandler) {
    this.handlers.set(name, handler)
  }

  async enqueue<T>(input: {
    name: string
    payload: T
    idempotencyKey: string
    signal?: AbortSignal
  }): Promise<TaskJob<T>> {
    this.prune()
    const existingId = this.byKey.get(input.idempotencyKey)
    if (existingId) {
      const existing = this.jobs.get(existingId)
      if (existing) {
        await this.pending.get(existingId)
        return structuredClone(this.jobs.get(existingId)!) as TaskJob<T>
      }
    }
    if (this.active >= (this.options.maxPending ?? 32)) {
      throw new Error('队列已满，请稍后重试')
    }
    if (this.jobs.size >= (this.options.maxRecords ?? 10000)) {
      throw new Error('幂等记录容量已满，请稍后重试')
    }
    input.signal?.throwIfAborted()
    const createdAt = now()
    const job: TaskJob<T> = {
      id: randomUUID(),
      name: input.name,
      payload: structuredClone(input.payload),
      idempotencyKey: input.idempotencyKey,
      status: 'queued',
      createdAt,
      updatedAt: createdAt,
    }
    this.jobs.set(job.id, job as TaskJob)
    this.byKey.set(input.idempotencyKey, job.id)
    this.queue.push(job.id)

    this.active++
    this.owned.add(job.id)
    this.executing.add(job.id)
    console.info('[queue] queued', job.id)
    const pending = (async () => {
      if (this.options.async) {
        await new Promise<void>((resolve) => setImmediate(resolve))
      }
      await this.consumeOne(job.id, input.signal)
    })()
    this.pending.set(job.id, pending)
    try {
      await pending
    } finally {
      this.pending.delete(job.id)
      this.owned.delete(job.id)
    }
    return structuredClone(this.jobs.get(job.id)!) as TaskJob<T>
  }

  private async consumeOne(jobId: string, signal?: AbortSignal) {
    const job = this.jobs.get(jobId)
    if (!job || job.status !== 'queued') {
      return
    }
    const handler = this.handlers.get(job.name)
    job.status = 'running'
    job.updatedAt = now()
    this.jobs.set(jobId, structuredClone(job))
    const qi = this.queue.indexOf(jobId)
    if (qi >= 0) {
      this.queue.splice(qi, 1)
    }
    if (!handler) {
      this.active--
      this.executing.delete(jobId)
      await this.fail(jobId, { code: 'NO_HANDLER', message: `未注册任务处理器：${job.name}` })
      return
    }
    const controller = new AbortController()
    const abort = () => controller.abort(signal?.reason)
    signal?.addEventListener('abort', abort, { once: true })
    if (signal?.aborted) {
      abort()
    }
    let timer: ReturnType<typeof setTimeout> | undefined
    let onAbort: (() => void) | undefined
    const work = Promise.resolve()
      .then(() => {
        controller.signal.throwIfAborted()
        return handler(job.payload, { job: structuredClone(job), signal: controller.signal })
      })
      .finally(() => {
        this.active--
        this.executing.delete(jobId)
      })
    try {
      const result = await Promise.race([
        work,
        new Promise<never>((_resolve, reject) => {
          onAbort = () => reject(controller.signal.reason ?? new Error('任务已取消'))
          controller.signal.addEventListener('abort', onAbort, { once: true })
          if (controller.signal.aborted) {
            onAbort()
          }
          timer = setTimeout(
            () => controller.abort(new Error('任务超时')),
            this.options.timeoutMs ?? 30000,
          )
        }),
      ])
      signal?.throwIfAborted()
      await this.complete(jobId, result)
    } catch (error: unknown) {
      if (signal?.aborted) {
        const j = this.jobs.get(jobId)
        if (j) {
          j.status = 'cancelled'
          j.error = { code: 'CANCELLED', message: '任务已取消' }
          j.updatedAt = now()
          this.jobs.set(jobId, structuredClone(j))
        }
        throw error
      }
      await this.fail(jobId, {
        code: 'HANDLER_FAILED',
        message: error instanceof Error ? error.message : '任务失败',
      })
      throw error
    } finally {
      clearTimeout(timer)
      signal?.removeEventListener('abort', abort)
      if (onAbort) {
        controller.signal.removeEventListener('abort', onAbort)
      }
      console.info('[queue]', this.jobs.get(jobId)?.status, jobId)
    }
  }

  async dequeue(opts?: { name?: string; timeoutMs?: number }): Promise<TaskJob | null> {
    const started = Date.now()
    for (;;) {
      const idx = this.queue.findIndex((id) => {
        const j = this.jobs.get(id)
        if (!j || j.status !== 'queued' || this.owned.has(id)) {
          return false
        }
        if (opts?.name && j.name !== opts.name) {
          return false
        }
        return true
      })
      if (idx >= 0) {
        const id = this.queue.splice(idx, 1)[0]!
        const job = this.jobs.get(id)!
        job.status = 'running'
        job.updatedAt = now()
        this.jobs.set(id, structuredClone(job))
        return structuredClone(job)
      }
      if (!opts?.timeoutMs || Date.now() - started >= opts.timeoutMs) {
        return null
      }
      await sleep(10)
    }
  }

  async complete(jobId: string, result: unknown) {
    const job = this.jobs.get(jobId)
    if (!job) {
      throw new Error(`job 不存在: ${jobId}`)
    }
    if (job.status === 'cancelled' || job.status === 'failed') {
      return structuredClone(job)
    }
    job.status = 'completed'
    job.result = structuredClone(result)
    job.updatedAt = now()
    this.jobs.set(jobId, structuredClone(job))
    return structuredClone(job)
  }

  async fail(jobId: string, error: { code: string; message: string }) {
    const job = this.jobs.get(jobId)
    if (!job) {
      throw new Error(`job 不存在: ${jobId}`)
    }
    job.status = 'failed'
    job.error = { ...error }
    job.updatedAt = now()
    this.jobs.set(jobId, structuredClone(job))
    return structuredClone(job)
  }

  async get(jobId: string) {
    this.prune()
    const j = this.jobs.get(jobId)
    return j ? structuredClone(j) : null
  }

  async getByIdempotencyKey(key: string) {
    this.prune()
    const id = this.byKey.get(key)
    return id ? this.get(id) : null
  }

  /** @deprecated 委托 enqueue，同 idempotencyKey 不双跑 */
  async execute<T>(
    taskId: string,
    job: (signal: AbortSignal) => Promise<T>,
    signal: AbortSignal,
  ): Promise<T> {
    this.register(`__inline__${taskId}`, async (_payload, ctx) => job(ctx.signal ?? signal))
    const result = await this.enqueue({
      name: `__inline__${taskId}`,
      payload: {},
      idempotencyKey: taskId,
      signal,
    })
    if (result.status === 'failed') {
      throw new Error(result.error?.message ?? '任务失败')
    }
    if (result.status === 'cancelled') {
      signal.throwIfAborted()
      throw new Error('任务已取消')
    }
    return result.result as T
  }
}

export class MemoryRateLimiter implements RateLimiter {
  private windows = new Map<string, { start: number; count: number }>()
  private running = new Map<string, number>()
  constructor(
    private options: {
      requests?: number
      windowMs?: number
      concurrency?: number
      clock?: () => number
      maxKeys?: number
    } = {},
  ) {}
  async allow(key: string, cost = 1) {
    if (!Number.isFinite(cost) || cost <= 0) {
      throw new Error('限流 cost 必须为正数')
    }
    const now = this.options.clock?.() ?? Date.now()
    const windowMs = this.options.windowMs ?? 60000
    // 每次访问也回收其他 key 的过期窗口；容量满时拒绝新增 key，避免无界增长。
    for (const [id, entry] of this.windows) {
      if (now - entry.start >= windowMs) {
        this.windows.delete(id)
      }
    }
    if (!this.windows.has(key) && this.windows.size >= (this.options.maxKeys ?? 10000)) {
      return { ok: false, retryAfterMs: windowMs }
    }
    let window = this.windows.get(key)
    if (!window || now - window.start >= windowMs) {
      window = { start: now, count: 0 }
      this.windows.set(key, window)
    }
    if (window.count + cost > (this.options.requests ?? 60)) {
      console.info('[limit] requests', key)
      return { ok: false, retryAfterMs: Math.max(1, windowMs - (now - window.start)) }
    }
    window.count += cost
    return { ok: true }
  }
  async acquire(key: string) {
    const count = this.running.get(key) ?? 0
    if (count >= (this.options.concurrency ?? 2)) {
      console.info('[limit] concurrency', key)
      return { ok: false, retryAfterMs: 1000 }
    }
    this.running.set(key, count + 1)
    return { ok: true }
  }
  release(key: string) {
    const count = this.running.get(key) ?? 0
    if (count <= 1) {
      this.running.delete(key)
    } else {
      this.running.set(key, count - 1)
    }
  }
}
export class MemoryAsyncQueue extends MemoryTaskQueue {
  constructor(
    options: {
      timeoutMs?: number
      maxPending?: number
      retentionMs?: number
      maxRecords?: number
    } = {},
  ) {
    super({ ...options, async: true })
  }
}

export class StubLlmProvider implements LlmProvider {
  constructor(private delayMs = 35) {}
  async act(input: { agent: 'host' | 'planner'; messages: unknown[]; tools: unknown[] }) {
    const prompt = extractPrompt(input.messages)
    const complex =
      /广告|镜头|套图|分镜|多段视频|(?:[2-9]|[二两三四五六七八九多])\s*段.*视频|视频.*(?:[2-9]|[二两三四五六七八九多])\s*段/.test(
        prompt,
      )
    if (this.delayMs > 0) {
      await sleep(this.delayMs)
    }
    const summary = input.messages.some(
      (m) =>
        m &&
        typeof m === 'object' &&
        (m as { role?: string; content?: string }).role === 'system' &&
        (m as { content?: string }).content === 'summary',
    )
    const finished = input.messages.some(
      (m) => m && typeof m === 'object' && (m as { content?: string }).content === 'finish',
    )
    if (finished && input.agent === 'planner') {
      return { toolCalls: [{ id: randomUUID(), name: 'handoff_to_host', args: { prompt } }] }
    }
    if (summary) {
      return { text: '\n所有镜头画面已完成，可点击作品放大查看。' }
    }
    if (input.agent === 'planner') {
      return {
        text: '\n将按三个镜头推进，生成前会请你确认积分。',
        toolCalls: ['咖啡豆特写', '冲煮过程', '晨光咖啡杯'].map((scene, index) => ({
          id: randomUUID(),
          name: 'creative_agent_mcp_gen_text2image_v3',
          args: { prompt: `${prompt}；镜头${index + 1}：${scene}` },
        })),
      }
    }
    if (complex) {
      return {
        text: '我先把需求交给策划，拆解镜头与任务计划。',
        toolCalls: [{ id: randomUUID(), name: 'handoff_to_planner', args: { prompt } }],
      }
    }
    return {
      text: '好的，我来为你生成一张创作图片。',
      toolCalls: [
        { id: randomUUID(), name: 'creative_agent_mcp_gen_text2image_v3', args: { prompt } },
      ],
    }
  }
}

function extractPrompt(messages: unknown[]): string {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i]
    if (m && typeof m === 'object' && (m as { role?: string }).role === 'user') {
      const content = (m as { content?: unknown }).content
      if (typeof content === 'string') {
        return content
      }
    }
  }
  const last = messages[messages.length - 1]
  if (typeof last === 'string') {
    return last
  }
  return ''
}

export class MemoryTraceWriter implements TraceWriter {
  private traces = new Map<string, AgentTraceV1>()
  async write(trace: AgentTraceV1) {
    this.traces.set(trace.runId, structuredClone(trace))
    return { path: `memory://${trace.runId}` }
  }
  get(runId: string) {
    const trace = this.traces.get(runId)
    return trace ? structuredClone(trace) : undefined
  }
}
