import type { AgentTraceTurnV1 } from '../trace/schema.js'
export type Part =
  | { kind: 'text'; text: string }
  | {
      kind: 'tool_status'
      callId: string
      toolName: string
      label: string
      state: 'loading' | 'completed' | 'failed' | 'cancelled'
    }
  | {
      kind: 'artifact'
      id: string
      type: 'image' | 'video'
      url: string
      status: 'success' | 'failed'
    }
  | {
      kind: 'plan'
      explanation?: string
      steps: { id: string; text: string; status: TodoPlanStep['status'] }[]
    }
  | {
      kind: 'confirm'
      confirmId: string
      title: string
      estimateCredits: number
      summary: string
      state: 'pending' | 'accepted' | 'rejected' | 'expired'
    }
  | { kind: 'error'; message: string }
export interface AssistantMessage {
  id: string
  role: 'assistant'
  status: 'streaming' | 'completed' | 'failed' | 'cancelled'
  parts: Part[]
}
/** 运行时 Turn（SSE / Host）；持久化见 TurnRecord */
export interface Turn {
  admissionKey?: string
  conversationId: string
  turnId: string
  runId: string
  prompt: string
  status: 'running' | 'interrupted' | 'completed' | 'failed' | 'cancelled'
  message: AssistantMessage
  createdAt: string
  updatedAt?: string
  execution?: ExecutionState
}
export type EventData =
  | {
      kind: 'system'
      type: 'stream_start' | 'stream_complete' | 'stream_error'
      status?: Turn['status']
      message?: string
    }
  | {
      kind: 'system'
      type: 'todo_plan'
      explanation?: string
      steps: Extract<Part, { kind: 'plan' }>['steps']
    }
  | {
      kind: 'system'
      type: 'interrupt_approval'
      confirmId: string
      title: string
      estimateCredits: number
      summary: string
    }
  | { kind: 'text_delta'; text: string }
  | { kind: 'message'; message: AssistantMessage }
/** SSE 写出载荷：sequence = conversation 级 seq */
export type LoggedEvent = EventData & {
  conversation_id: string
  turn_uuid: string
  runId: string
  sequence: number
  timestamp: string
}
export interface ToolCall {
  id: string
  name: string
  args: unknown
}
export type LlmChunk = { kind: 'text'; text: string } | { kind: 'tool_call'; call: ToolCall }
export interface GeneratedToolResult {
  need_user?: false
  artifact: Extract<Part, { kind: 'artifact' }>
  credits: number
}
export type ToolResult =
  | GeneratedToolResult
  | { need_user: true; estimateCredits: number; summary: string; pendingToolCall?: ToolCall }
export interface ExecutionState {
  phase: 'host' | 'planner' | 'generating' | 'summary' | 'done'
  activeAgent: 'host' | 'planner'
  calls: ToolCall[]
  index: number
  approvedCallIds: string[]
  plan?: TodoPlan
  agents: { host: number; planner: number }
  toolCalls: AgentTraceTurnV1['toolCalls']
  interrupts: AgentTraceTurnV1['interrupts']
  credits: number
}
export interface Span {
  name: string
  startedAt: string
  endedAt: string
  status: 'completed' | 'failed' | 'cancelled'
  detail?: string
}

// ---- S1 ports 领域记录（PORTS_AND_TRACE）----
export interface ContinuationDraft {
  sourceTurnId: string
  originalPrompt: string
  execution: ExecutionState
  messagePartsUseful?: Part[]
  savedAt: string
}
export interface ConversationRecord {
  id: string
  continuationDraft?: ContinuationDraft
  userId?: string
  title?: string
  activeAgent: 'host' | 'planner'
  createdAt: string
  updatedAt: string
}
export interface InterruptSnapshot {
  confirmId: string
  reason: 'credits' | 'approval' | string
  estimateCredits?: number
  summary?: string
  activeAgent: 'host' | 'planner'
  pendingToolCall?: { id: string; name: string; args: unknown }
  cursor?: { lastEventSeq: number; messageId?: string }
  savedAt: string
  /** S3 可序列化续执行位置；不能依赖 Host 实例变量。 */
  continuation?: ExecutionState
}
export interface TodoPlanStep {
  id: string
  title: string
  status: 'pending' | 'in_progress' | 'completed' | 'failed' | 'cancelled'
}
export interface TodoPlan {
  explanation?: string
  steps: TodoPlanStep[]
}
export interface TurnRecord {
  id: string
  conversationId: string
  status: 'running' | 'interrupted' | 'completed' | 'failed' | 'cancelled'
  snapshot?: InterruptSnapshot | null
  plan?: TodoPlan | null
  createdAt: string
  updatedAt: string
  /** 运行时扩展字段，不进严格 TurnRecord 契约也可保留 */
  runId?: string
  prompt?: string
  message?: AssistantMessage
  execution?: ExecutionState
  resolutions?: { confirmId: string; approved: boolean; state: 'running' | 'settled' }[]
}
export interface AgentEvent {
  seq: number
  conversationId: string
  turnId: string
  at: string
  event: 'system' | 'delta' | 'message'
  data: unknown
}
export interface TaskJob<T = unknown> {
  id: string
  name: string
  payload: T
  idempotencyKey: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  result?: unknown
  error?: { code: string; message: string }
  createdAt: string
  updatedAt: string
}
