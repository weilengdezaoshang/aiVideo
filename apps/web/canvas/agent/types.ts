import type { AgentClaim, AgentReflectionIssue, AgentReflectionVerdict } from './contracts.js'

export type AgentRunStatus =
  | 'queued'
  | 'running'
  | 'completed'
  | 'stopped'
  | 'failed'
  | 'needs_attention'
  | 'limit_reached'
  | 'waiting_input'

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
  stepKey?: string | null
  name: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled' | 'late'
  arguments: Record<string, unknown>
  output?: unknown
  error?: string | null
}

export type AgentStepStatus = 'pending' | 'running' | 'completed' | 'blocked' | 'obsolete'
export type AgentStep = {
  id: string
  planVersion: number
  key: string
  ordinal: number
  title: string
  intent: string
  tools: string[]
  dependsOn: string[]
  doneWhen: string
  status: AgentStepStatus
  summary: string | null
  evidenceRefs: string[]
  retries: number
}
export type AgentRunPlan = { goal: string; version: number; current: string | null }
export type AgentWaiting = { question: string; stage: string }
export type AgentReflectionSource = 'blocker' | 'hard_check' | 'model'
export type AgentReflectionRecord = {
  id: string
  round: number
  source: AgentReflectionSource
  verdict: AgentReflectionVerdict
  issues: AgentReflectionIssue[]
  question: string | null
  hardChecks: AgentVerification | null
  createdAt: string
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
  persona?: { id: string; version: number; displayName: string } | null
  modelId: string
  baseRevision: number
  stage?: string | null
  plan?: AgentRunPlan | null
  waiting?: AgentWaiting | null
  steps?: AgentStep[]
  reflections?: AgentReflectionRecord[]
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
