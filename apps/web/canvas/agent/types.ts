import type { AgentClaim } from './contracts.js'

export type AgentRunStatus =
  'queued' | 'running' | 'completed' | 'stopped' | 'failed' | 'needs_attention' | 'limit_reached'

export type AgentClaimsPayload = {
  answer: string
  claims: AgentClaim[]
  verification: AgentVerification
}
export type AgentVerificationIssue = {
  check: string
  code: string
  detail: string
  claim: number | null
  ref: string | null
}
export type AgentVerification = {
  passed: boolean
  checks: { id: string; passed: boolean; issues: AgentVerificationIssue[] }[]
}

export type AgentToolCall = {
  id: string
  evidenceId?: string | null
  kind?: 'read' | 'control'
  dedupeOf?: string | null
  name: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled' | 'late'
  arguments: Record<string, unknown>
  output?: unknown
  error?: string | null
}

export type AgentModelCall = {
  id: string
  ordinal: number
  status: string
  usage?: Record<string, unknown> | null
  error?: string | null
}

export type AgentRun = {
  id: string
  clientRequestId: string
  status: AgentRunStatus
  message: string
  reply: string | null
  error: string | null
  errorCode?: string | null
  claims?: AgentClaimsPayload | null
  modelId: string
  baseRevision: number
  modelCalls: AgentModelCall[]
  toolCalls: AgentToolCall[]
  modelCallCount?: number
  toolCallCount?: number
  createdAt: string
  updatedAt: string
  stateVersion?: number
}

export type AgentModel = { id: string; provider: string; model: string; label: string }
export type AgentModels = { models: AgentModel[]; defaultModelId: string | null }
export type AgentRequest = {
  clientRequestId: string
  message: string
  modelId: string
  baseRevision: number
  selectedObjectIds: string[]
}
export type AgentSnapshot = {
  sessionId: string | null
  documentId: string
  cursor: number
  runs: AgentRun[]
  requestedRun?: AgentRun | null
}
export type AgentEvent = { seq: number; runId: string; type: string; run: AgentRun }

export function isActiveAgentRun(run: AgentRun): boolean {
  return run.status === 'queued' || run.status === 'running'
}
