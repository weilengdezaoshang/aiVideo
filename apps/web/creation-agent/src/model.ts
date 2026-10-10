export type Step = {
  id: string
  text: string
  status: 'pending' | 'in_progress' | 'completed' | 'cancelled' | 'failed'
}
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
  | { kind: 'plan'; explanation?: string; steps: Step[] }
  | {
      kind: 'confirm'
      confirmId: string
      title: string
      estimateCredits: number
      summary: string
      state: 'pending' | 'accepted' | 'rejected' | 'expired'
    }
  | { kind: 'error'; message: string }
export interface Message {
  id: string
  role: 'user' | 'assistant'
  status: 'streaming' | 'completed' | 'failed' | 'cancelled'
  parts: Part[]
}
export type SessionStatus = 'idle' | 'sending' | 'streaming' | 'awaiting_user'
export type EventPayload =
  | { type: 'stream_start' | 'text_phase' }
  | { type: 'stream_complete'; status?: 'completed' | 'cancelled' }
  | { type: 'text_delta'; text: string }
  | { type: 'tool_start'; callId: string; label: string }
  | { type: 'tool_success'; callId: string; artifact: Extract<Part, { kind: 'artifact' }> }
  | { type: 'tool_failure'; callId: string; message: string }
  | { type: 'plan_update'; steps: Step[]; explanation?: string }
  | { type: 'confirm_required'; confirm: Extract<Part, { kind: 'confirm' }> }
  | { type: 'stream_error'; message: string }
  | { type: 'message_snapshot'; message: Message }
export type StreamEvent = EventPayload & { runId: string }
export interface Faults {
  connection: boolean
  tool: boolean
  resume: boolean
}
export interface Transport {
  remoteInterrupts?: boolean
  start(
    runId: string,
    complex: boolean,
    signal: AbortSignal,
    emit: (event: StreamEvent) => void,
    faults: Faults,
    prompt?: string,
  ): Promise<void>
  resumeInterrupt(
    runId: string,
    confirmId: string,
    signal: AbortSignal,
    faults: Faults,
    approved?: boolean,
  ): Promise<void>
  stop(runId: string): void | Promise<void>
}
