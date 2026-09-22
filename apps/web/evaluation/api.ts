/** /api/evals/v1 类型化客户端;禁止 any,所有载荷显式建模。 */

export interface DatasetVersionInfo {
  version: number
  split: string
  caseCount: number
  contentHash: string
  frozenAt: string
}

export interface DatasetInfo {
  datasetId: string
  versions: DatasetVersionInfo[]
}

export interface RunListItem {
  runId: string
  createdAt: string
  mode: string
  provider: string
  purpose: string
  legacy: boolean
  status: string | null
  planCount: number
  planHash: string
}

export interface Denominators {
  plan: number
  attempted: number
  generated: number
  determined: number
  pass: number
}

export interface RunProgress {
  total: number
  pending: number
  running: number
  completed: number
  failed: number
  cancelled: number
  timedOut: number
}

export interface ReplayVerification {
  matchProtocolVersion: number
  matched: number
  unconsumedInPlan: number[]
  unconsumedOutOfPlan: number[]
  newExternalCalls: number
  newExternalCallDetail: { method: string; url: string }[]
  verification: string
}

export interface RunDetail {
  runId: string
  manifest: {
    purpose: string
    mode: string
    provider: string
    dataset: { datasetId: string; version: number; contentHash: string; frozenAt: string | null }
    planCount: number
    grading: { judge: string; model?: string }
    legacy: boolean
    parentRunId: string | null
    recordingId: string | null
    compression: { enabled: boolean; compressorVersion?: string }
  }
  status: string
  progress: RunProgress
  extra: {
    replay?: ReplayVerification
    budget?: {
      settled: number
      outstandingReserved: number
      unknownCount: number
      totalCalls: number
    }
    error?: string
    recordingId?: string
    recordingSealed?: boolean
    traceId?: string
  }
  denominators: Denominators
  metrics: Record<string, number | null>
  annotations: string[]
  coverageGaps: string[]
}

export interface GradeView {
  gradeId: string
  checkId: string
  status: string
  observed: unknown
  expected: unknown
  score: number | null
  source: string
  cacheHit?: boolean
}

export interface TrialView {
  trialId: string
  caseId: string
  repetitionIndex: number
  status: string
  qualityVerdict: string | null
  weightedScore: number | null
  requestedSeed: number
  resolvedSeed: number | null
  error: string | null
  artifactIds: string[]
  grades: GradeView[]
}

export interface AttemptView {
  attemptId: string
  trialId: string
  callSite: string
  attemptIndex: number
  status: string
  usage: { source: string; textTokens: number | null; raw?: Record<string, unknown> } | null
  cost: { currency: string; amount: number | null; basis: string } | null
  externalTaskId: string | null
  error: string | null
}

export interface ArtifactView {
  artifactId: string
  mime: string
  size?: number
  kind?: string
  missing?: boolean
}

export interface TrialDetailView {
  runId: string
  trial: TrialView
  steps: StepEvent[]
  grades: GradeView[]
  attempts: AttemptView[]
  artifacts: ArtifactView[]
}

export interface StepEvent {
  eventId: string
  sequence: number
  type: string
  trialId: string | null
  stepId: string | null
  timestamp: string
  payload: Record<string, unknown>
}

export interface GateView {
  gateId: string
  verdict: string
  reasons: string[]
  policyId: string
  policyVersion: number
  decidedAt: string
  inputWatermark: Record<string, unknown>
}

export interface ReservationView {
  reservationId: string
  callSite: string
  status: string
  reservedAmount: number | null
  settledAmount: number | null
  settledBasis: string | null
  trialId: string | null
  note: string | null
}

export interface BudgetSummary {
  scopeId: string
  settled: number
  settledBilled: number
  settledEstimated: number
  outstandingReserved: number
  unknownCount: number
  totalCalls: number
  currency: string
  maxCost: number | null
  maxExternalCalls: number | null
  priceVersion: string
  reservations?: ReservationView[]
  project?: {
    scopeId: string
    settled: number
    outstandingReserved: number
    unknownCount: number
    totalCalls: number
  }
}

export interface ReviewTaskView {
  reviewTaskId: string
  runId: string
  trialId: string
  caseId: string
  status: string
  revision: number
  priority: string
  claim: { reviewer: string; leaseUntil: string } | null
  sampling: { rule: string; policyVersion: string | null }
  artifactIds: string[]
}

export interface ReviewOpinionView {
  opinionId: string
  reviewer: string
  revision: number
  verdicts: { checkId: string; verdict: string; note?: string | null }[]
  agreeWithAuto: boolean | null
  category: string | null
  note: string | null
  uncertain: boolean
  supersededBy: string | null
  submittedAt: string
}

export interface AdjudicationView {
  adjudicationId: string
  boundOpinionIds: string[]
  finalVerdict: string
  reason: string
  decidedBy: string
  rubricVersion: string
  at: string
}

export interface ReviewTaskDetail {
  task: ReviewTaskView
  opinions: ReviewOpinionView[]
  disputes: { by: string; reason: string; at: string }[]
  adjudications: AdjudicationView[]
}

export interface ComparisonView {
  comparability: string
  note: string
  configDifferences: string[]
  subjectDeltas: string[]
  independentSamples: boolean
  checkRegressions: {
    caseId: string
    checkId: string
    baselineStatus: string
    candidateStatus: string
    baselineGradeId: string
    candidateGradeId: string
    candidateTrialId: string
  }[]
  candidateErrorCategories: { category: string; trialIds: string[] }[]
  coverage: { pairedCount: number; onlyInBaseline: string[]; onlyInCandidate: string[] }
  summary: {
    baselinePassRate: number | null
    candidatePassRate: number | null
    overallDelta: number | null
    regressions: { caseId: string; delta: number }[]
    improvements: { caseId: string; delta: number }[]
    uncertainty: string
  }
  paired: {
    caseId: string
    baseline: { status: string; verdict: string | null; score: number | null }
    candidate: { status: string; verdict: string | null; score: number | null }
    delta: number | null
  }[]
}

export interface HealthView {
  activeRuns: number
  stuckTrials: string[]
  unknownExternalTrials: number
  budget: { unsettledReservations: number; unknownOutcomes: number }
  judgeErrorTrials: number
  judgedTrials: number
  oldestPendingReviewHours: number | null
  storeBytes: number
  note: string
}

export interface AlertView {
  rules: Record<string, unknown>
  alerts: { code: string; message: string; evidence?: unknown }[]
  note: string
}

export interface OutboxStatus {
  pending: number
  langfuseConfigured: boolean
  note: string
}

export interface OutboxFlushResult {
  flushed: number
  remaining: number
  configured?: boolean
  lastError?: string
}

export interface CompressionReport {
  baselineRunId: string
  compressedRunId: string
  compressorVersion: string | null
  compressionEnabled: boolean
  quality: {
    baselinePassRate: number | null
    compressedPassRate: number | null
    delta: number | null
    note: string
  }
  tokens: {
    baseline: { providerReportedTokens: number | null }
    compressed: { providerReportedTokens: number | null }
    delta: number | null
    note: string
  }
  cost: {
    baselineSettled: number | null
    compressedSettled: number | null
    delta: number | null
    note: string
  }
  promote: boolean | null
  promoteReason?: string
}

export interface CalibrationView {
  calibrationId: string
  judgeModel: string
  rubricVersion: string
  sampleCount: number
  splits: Record<string, number>
  report: {
    defectRecall: number | null
    falsePositiveRate: number | null
    confusion: { TP: number; FN: number; FP: number; TN: number }
    excluded: { autoUnknown: number; humanUnknown: number }
  }
  createdAt: string
}

export interface CreateRunBody {
  runId: string
  datasetId?: string
  version?: number
  repetitions?: number
  purpose?: string
  mode?: 'mock' | 'live' | 'replay'
  provider?: string
  judge?: string
  judgeConfig?: Record<string, unknown>
  budget?: Record<string, unknown>
  sandboxConfig?: Record<string, string>
  compression?: { enabled: boolean; compressorVersion?: string }
  useGradeCache?: boolean
  model?: string
  sourceRunId?: string
  recordingId?: string
}

export interface CreateRunResult {
  runId: string
  status: string
  planCount: number
  planHash: string
  queued: boolean
  queueReason: string | null
  note: string
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message)
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { 'content-type': 'application/json' },
    ...init,
  })
  const text = await response.text()
  const payload: unknown = text ? JSON.parse(text) : {}
  if (!response.ok) {
    const message =
      typeof payload === 'object' && payload !== null && 'error' in payload
        ? String((payload as { error: unknown }).error)
        : `请求失败(${response.status})`
    throw new ApiError(response.status, message)
  }
  return payload as T
}

export const api = {
  datasets: () => request<{ datasets: DatasetInfo[] }>('/api/evals/v1/datasets'),
  runs: () => request<{ runs: RunListItem[] }>('/api/evals/v1/runs'),
  run: (runId: string) => request<RunDetail>(`/api/evals/v1/runs/${encodeURIComponent(runId)}`),
  trials: (runId: string) =>
    request<{ trials: TrialView[] }>(`/api/evals/v1/runs/${encodeURIComponent(runId)}/trials`),
  events: (runId: string, after = 0) =>
    request<{ events: StepEvent[] }>(
      `/api/evals/v1/runs/${encodeURIComponent(runId)}/events?after=${after}`,
    ),
  report: (runId: string) =>
    request<Record<string, unknown>>(`/api/evals/v1/runs/${encodeURIComponent(runId)}/report`),
  createReport: (runId: string) =>
    request<{ reportVersion: number }>(`/api/evals/v1/runs/${encodeURIComponent(runId)}/report`, {
      method: 'POST',
      body: '{}',
    }),
  gate: (runId: string) =>
    request<GateView>(`/api/evals/v1/runs/${encodeURIComponent(runId)}/gate`, {
      method: 'POST',
      body: '{}',
    }),
  gates: (runId: string) =>
    request<{ gates: GateView[] }>(`/api/evals/v1/runs/${encodeURIComponent(runId)}/gates`),
  budget: (runId: string) =>
    request<BudgetSummary>(`/api/evals/v1/runs/${encodeURIComponent(runId)}/budget`),
  createRun: (body: CreateRunBody) =>
    request<CreateRunResult>('/api/evals/v1/runs', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  cancelRun: (runId: string) =>
    request<{ runId: string; status: string; cancelRequested: boolean }>(
      `/api/evals/v1/runs/${encodeURIComponent(runId)}/cancel`,
      { method: 'POST', body: '{}' },
    ),
  compare: (baselineRunId: string, candidateRunId: string) =>
    request<ComparisonView>('/api/evals/v1/comparisons', {
      method: 'POST',
      body: JSON.stringify({ baselineRunId, candidateRunId }),
    }),
  compressionReport: (baselineRunId: string, compressedRunId: string) =>
    request<CompressionReport>('/api/evals/v1/compression-report', {
      method: 'POST',
      body: JSON.stringify({ baselineRunId, compressedRunId }),
    }),
  health: () => request<HealthView>('/api/evals/v1/health'),
  alerts: () => request<AlertView>('/api/evals/v1/alerts'),
  outboxStatus: () => request<OutboxStatus>('/api/evals/v1/outbox/status'),
  outboxFlush: () =>
    request<OutboxFlushResult>('/api/evals/v1/outbox/flush', { method: 'POST', body: '{}' }),
  calibrations: () => request<{ calibrations: CalibrationView[] }>('/api/evals/v1/calibrations'),
  reviewTasks: (status?: string, runId?: string) => {
    const params = new URLSearchParams()
    if (status) {
      params.set('status', status)
    }
    if (runId) {
      params.set('run_id', runId)
    }
    const query = params.toString()
    return request<{ tasks: ReviewTaskView[] }>(
      `/api/evals/v1/review-tasks${query ? `?${query}` : ''}`,
    )
  },
  reviewTaskDetail: (taskId: string) =>
    request<ReviewTaskDetail>(`/api/evals/v1/review-tasks/${encodeURIComponent(taskId)}`),
  createReviewTask: (body: Record<string, unknown>) =>
    request<{ task: ReviewTaskView }>('/api/evals/v1/review-tasks', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  claimReview: (taskId: string, reviewer: string, expectedRevision: number) =>
    request<{ task: ReviewTaskView }>(`/api/evals/v1/review-tasks/${taskId}/claim`, {
      method: 'POST',
      body: JSON.stringify({ reviewer, expectedRevision }),
    }),
  submitReview: (taskId: string, body: Record<string, unknown>) =>
    request<{ opinion: { opinionId: string } }>(`/api/evals/v1/review-tasks/${taskId}/reviews`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  disputeReview: (taskId: string, by: string, reason: string) =>
    request<{ task: ReviewTaskView }>(`/api/evals/v1/review-tasks/${taskId}/dispute`, {
      method: 'POST',
      body: JSON.stringify({ by, reason }),
    }),
  adjudicate: (taskId: string, body: Record<string, unknown>) =>
    request<{ adjudication: { adjudicationId: string } }>(
      `/api/evals/v1/review-tasks/${taskId}/adjudications`,
      { method: 'POST', body: JSON.stringify(body) },
    ),
  trialDetail: (trialId: string) =>
    request<TrialDetailView>(`/api/evals/v1/trials/${encodeURIComponent(trialId)}`),
  artifactUrl: (artifactId: string) =>
    `/api/evals/v1/artifacts/${encodeURIComponent(artifactId)}/content`,
  createCaseDraft: (taskId: string, caseData: Record<string, unknown>) =>
    request<{ draftId: string }>(`/api/evals/v1/review-tasks/${taskId}/case-drafts`, {
      method: 'POST',
      body: JSON.stringify({ case: caseData }),
    }),
  publishCaseDraft: (draftId: string, datasetId: string, split = 'regression') =>
    request<{ datasetId: string; version: number; contentHash: string }>(
      `/api/evals/v1/dataset-drafts/${encodeURIComponent(draftId)}/publish`,
      { method: 'POST', body: JSON.stringify({ datasetId, split }) },
    ),
}
