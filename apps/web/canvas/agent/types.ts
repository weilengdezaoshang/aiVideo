import type { AgentRequest as CanvasAgentRequest } from '../../shared/agent-protocol.js'
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
  | 'waiting_approval'
  | 'awaiting_confirm'
  | 'awaiting_user_final'
  | 'canceling'
  | 'cancelled'
  | 'blocked'

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
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled' | 'late' | 'waiting_approval'
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
/** What a parked Run waits for: a user answer, or approval of one effect tool call. */
export type AgentWaiting =
  | { question: string; stage: string; options?: { id: string; label: string }[] }
  | { tool: string; arguments: Record<string, unknown>; stage: string }
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

export type CanvasAgentAction = {
  type: 'cutout'
  documentId: string
  sourceObjectId: string
  sourceAssetId: string
  resultObjectId?: string
  resultAssetId?: string
}

export type AgentRun = {
  parent_run_id?: string | null
  intentTrace?: {
    multi_step: boolean
    steps: { id: string; l2: string }[]
    step_results: { id: string; l2: string; status: AgentStepStatus }[]
    step_fail_index?: number
  }
  canvasAction?: CanvasAgentAction
  id: string
  clientRequestId: string
  status: AgentRunStatus
  message: string
  reply: string | null
  error: string | null
  errorCode?: string | null
  claims?: AgentClaimsPayload | null
  persona?: { id: string; version: number; displayName: string } | null
  media?: {
    tier: 'preview' | 'final'
    goalType: 'image' | 'video'
    previewRunId: string | null
    quoteCredits: number
    pendingQuoteCredits?: number | null
    chargedCredits: number
    refundCredits: number
    completed: number
    total: number
    documentRevision: number | null
    cancelReason: string | null
    finalMode?: 'reference_image' | 'rerender' | null
    children: {
      stepId: string
      intentStepId?: string | null
      title: string
      retryCredits: number
      index: number
      status: string
      assetId: string | null
      errorCode: string | null
      targetNode: string
      attempt: number
    }[]
  }
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
  batch?: {
    completedItems: number
    settledItems?: number
    processedItems?: number
    totalItems: number
    failedCount: number
    blockedCount?: number
    skippedCount?: number
    retryableItemIds?: string[]
    settled?: boolean
  }
}

export type AgentModel = { id: string; provider: string; model: string; label: string }
export type AgentModels = {
  models: AgentModel[]
  defaultModelId: string | null
  hostEnabled?: boolean
}
export type AgentRequest = {
  context?: CanvasAgentRequest
  clientRequestId: string
  message: string
  modelId: string
  baseRevision: number
  selectedObjectIds: string[]
  batch?: boolean
  supersedeRunId?: string
  clarifyOptionId?: string
  clarifyRunId?: string
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
  return (
    run.status === 'queued' ||
    run.status === 'running' ||
    run.status === 'canceling' ||
    run.status === 'awaiting_confirm' ||
    (Boolean(run.media) && run.status === 'needs_attention')
  )
}
