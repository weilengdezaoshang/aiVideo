import type { Evidence } from '../agent/reflection.js'
import type { GeneratedToolResult } from '../domain/model.js'
import type {
  AgentEvent,
  ConversationRecord,
  ContinuationDraft,
  InterruptSnapshot,
  TaskJob,
  ToolResult,
  Turn,
  TurnRecord,
} from '../domain/model.js'
import type { AgentTraceV1 } from '../trace/schema.js'

export interface SessionStore {
  saveContinuationDraft(conversationId: string, draft: ContinuationDraft): Promise<void>
  getContinuationDraft(conversationId: string): Promise<ContinuationDraft | null>
  clearContinuationDraft(conversationId: string): Promise<void>
  getConversation(id: string): Promise<ConversationRecord | null>
  saveConversation(c: ConversationRecord): Promise<void>
  createConversation(init?: Partial<ConversationRecord>): Promise<ConversationRecord>

  getTurn(turnId: string): Promise<TurnRecord | null>
  listTurns(conversationId: string): Promise<TurnRecord[]>
  saveTurn(t: TurnRecord): Promise<void>

  getInterruptSnapshot(turnId: string): Promise<InterruptSnapshot | null>
  saveInterruptSnapshot(turnId: string, snap: InterruptSnapshot): Promise<void>
  clearInterruptSnapshot(turnId: string): Promise<void>

  /** S2 运行时：创建会话+轮次并挂 AbortController */
  beginTurn(prompt: string, conversationId?: string): Promise<Turn>
  getRuntimeTurn(conversationId: string, turnId: string): Promise<Turn | undefined>
  saveRuntimeTurn(turn: Turn): Promise<void>
  signal(turnId: string): AbortSignal
  cancel(conversationId: string, turnId: string): Promise<Turn | undefined>
  release(turnId: string): void
  /** 原子认领同一确认；重复请求不重新执行工具。 */
  claimInterrupt(
    turnId: string,
    confirmId: string,
    approved: boolean,
  ): Promise<'claimed' | 'running' | 'settled' | 'conflict'>
  activateTurn(turn: Turn): Promise<void>
  settleInterrupt(turnId: string, confirmId: string): Promise<void>
}

export interface EventLog {
  append(e: Omit<AgentEvent, 'seq' | 'at'> & { at?: string }): Promise<AgentEvent>
  readFrom(
    conversationId: string,
    seq: number,
    opts?: { limit?: number; turnId?: string },
  ): Promise<AgentEvent[]>
  getLastSeq(conversationId: string): Promise<number>
  /** convenience：按 turn 读（内部仍用 conversation 级 seq） */
  listByTurn(turnId: string): Promise<AgentEvent[]>
}

export type TaskHandler<T = unknown> = (
  payload: T,
  ctx: { job: TaskJob<T>; signal?: AbortSignal },
) => Promise<unknown>

export interface TaskQueue {
  register(name: string, handler: TaskHandler): void
  enqueue<T>(input: {
    name: string
    payload: T
    idempotencyKey: string
    signal?: AbortSignal
  }): Promise<TaskJob<T>>
  dequeue(opts?: { name?: string; timeoutMs?: number }): Promise<TaskJob | null>
  complete(jobId: string, result: unknown): Promise<TaskJob>
  fail(jobId: string, error: { code: string; message: string }): Promise<TaskJob>
  get(jobId: string): Promise<TaskJob | null>
  getByIdempotencyKey(key: string): Promise<TaskJob | null>
  /**
   * @deprecated 薄封装 → enqueue；仅兼容旧调用，不是唯一入口
   */
  execute?<T>(
    taskId: string,
    job: (signal: AbortSignal) => Promise<T>,
    signal: AbortSignal,
  ): Promise<T>
}

export interface RateLimiter {
  acquire?(key: string): Promise<{ ok: boolean; retryAfterMs?: number }>
  release?(key: string): void
  allow(key: string, cost?: number): Promise<{ ok: boolean; retryAfterMs?: number }>
}

export interface LlmProvider {
  act(input: {
    agent: 'host' | 'planner'
    messages: unknown[]
    tools: unknown[]
    temperature?: number
  }): Promise<{
    text?: string
    toolCalls?: { id: string; name: string; args: unknown }[]
  }>
}

export interface ReviewContext {
  conversationId?: string
  turnId?: string
  toolCallId?: string
}
export interface ReviewInput {
  artifact: { url: string; localPath?: string; fixtureId?: string }
  result: GeneratedToolResult
  prompt: string
  taskType: 'image' | 'video'
  rubricId: string
  rubric: string[]
  attempt: number
  context: ReviewContext
  signal: AbortSignal
}
export type ReviewResult = NonNullable<Evidence['review']>
export interface ReviewerProvider {
  review(input: ReviewInput): Promise<ReviewResult>
}

export interface TraceWriter {
  write(trace: AgentTraceV1): Promise<{ path: string }>
}

export interface Tool {
  /** Parallel workers must not return need_user; approvals happen before dispatch. */
  parallelSafe?: boolean
  adapter?: 'mock' | 'cloud' | 'comfyui'
  name: string
  estimateCredits?: number
  execute(
    input: {
      prompt: string
      approved?: boolean
      tier?: 'preview' | 'final'
      attempt?: number
      imageRefs?: string[]
      frameCallIds?: string[]
      frameUrls?: string[]
    },
    signal: AbortSignal,
  ): Promise<ToolResult>
}

export interface Ports {
  sessions: SessionStore
  events: EventLog
  queue: TaskQueue
  limiter: RateLimiter
  llm: LlmProvider
  reviewer?: ReviewerProvider
  traces: TraceWriter
}
