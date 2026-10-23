/** /api/evals/v1 类型化客户端;禁止 any,所有载荷显式建模。 */

export interface CaseDefinition {
  caseId: string
  taskType: string
  title?: string
  description?: string
  tags?: string[]
  suites: string[]
  input: { prompt: string; params: Record<string, unknown> }
  steps?: Record<string, unknown>[]
  expect?: Record<string, unknown>
  mocks?: Record<string, unknown>
  checks: {
    id: string
    question: string
    expected: unknown
    required: boolean
    weight: number
    dependsOn?: string[]
  }[]
}

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

/** 部署 route 的公开字段。不含 endpoint、credentialRef 或 apiKey。 */
export interface PlannerRoutePublic {
  id: string
  label: string
  provider: string
  model: string
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
  datasetId?: string | null
  compression?: boolean
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

export interface PlannerRate {
  hits: number
  denominator: number
  rate: number | null
}

export interface PlannerEvidence {
  llmTested: boolean
  llmUntestedReason: string | null
  plannerMode: string | null
  synthetic: boolean
  promptTemplateHash: string | null
  promptTemplateModule: string | null
  toolCatalogVersion: string | null
  toolCatalogHash: string | null
  datasetContentHash: string | null
  coverage: { scored: number; denominator: number; rate: number | null }
  tools: PlannerRate
  order: PlannerRate
  invariants: PlannerRate
  firstReviewPass: PlannerRate
  latencyMs: { n: number; p50: number | null; p95: number | null }
  tokens: { perCase: number[]; total: number | null; mean: number | null; denominator: number }
}

export interface FlowEvidence {
  passRate: number | null
  layers: Record<string, { plan: number; pass: number; fail: number }>
  failures: { suite: string; caseId: string; reason: string; tracePath: string | null }[]
}

export interface RunDetail {
  flow?: FlowEvidence | null
  planner?: PlannerEvidence | null
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
    reviewSummary?: {
      status: string
      error?: string
      count: number
      required: number
      enabled: boolean
      policyVersion: string
    }
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
  tracePath?: string | null
  latencyMs?: number | null
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
  evidence?: {
    suite: string | null
    reasons: string[]
    tracePath: string | null
    traceUrl: string | null
  }
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

/** 配对硬校验的中文原因;code 为稳定原码(snapshot_mismatch / missing_cost / ...)。 */
export interface PairReason {
  code: string
  message: string
}

export interface PairCoverage {
  alignedCases: number
  alignedCompletedCases: number
  baselinePlan: number
  compressedPlan: number
}

export interface PairCandidate {
  runId: string
  createdAt: string
  role: 'baseline' | 'compressed'
  valid: boolean
  reasons: PairReason[]
}

/** 新建评测预填:重跑引导(同数据集 + 反向压缩开关 + live 配置)传给表单。 */
export interface PairRunPrefill {
  datasetId: string
  version?: number
  mode: 'live'
  provider?: string
  model?: string | null
  budget?: Record<string, unknown>
  judge?: string
  judgeConfig?: Record<string, unknown>
  compression: { enabled: boolean }
  purpose: string
}

export interface PairSuggestion {
  currentRunId: string | null
  currentRole: 'baseline' | 'compressed' | null
  pairRunId: string | null
  candidates: PairCandidate[]
  reasons: PairReason[]
  guidance: {
    action: 'create_counterpart' | 'rerun_current_live'
    message: string
    prefill: PairRunPrefill
  } | null
  coverage: PairCoverage | null
}

export interface ValidatedPair {
  valid: true
  baselineRunId: string
  candidateRunId: string
  coverage: PairCoverage
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

export interface ReviewSamplingConfig {
  enabled: boolean
  required: 'fail_or_undetermined' | 'none' | 'all'
  riskRate: number
  randomRate: number
  riskThreshold: number
  riskMargin: number
  grayMin: number
  grayMax: number
  gateScoreThreshold: number | null
  reviewAgentFlowFails: boolean
  includeSynthetic: boolean
}
export interface CreateRunBody {
  reviewSampling?: ReviewSamplingConfig
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
  readonly reasons?: PairReason[]
  constructor(
    readonly status: number,
    message: string,
    reasons?: PairReason[],
  ) {
    super(message)
    this.reasons = reasons
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
    const record = (typeof payload === 'object' && payload !== null ? payload : {}) as {
      error?: unknown
      detail?: unknown
      reasons?: unknown
    }
    // 配对硬校验的 4xx:{"error": 中文总结, "reasons": [{code, message}]},reasons 单独保留供列表渲染。
    const reasons = Array.isArray(record.reasons)
      ? (record.reasons as PairReason[]).filter(
          (item): item is PairReason =>
            typeof item === 'object' && item !== null && 'code' in item && 'message' in item,
        )
      : undefined
    const detailMessage =
      typeof record.error === 'string' && record.error
        ? record.error
        : typeof record.detail === 'string'
          ? record.detail
          : undefined
    const message =
      detailMessage ??
      (reasons?.length
        ? reasons.map((item) => item.message).join('；')
        : `请求失败(${response.status})`)
    throw new ApiError(response.status, message, reasons)
  }
  return payload as T
}

export const api = {
  flowCases: (suite = 'agent_flow') =>
    request<{ cases: CaseDefinition[] }>(
      `/api/evals/v1/agent-flow/cases?suite=${encodeURIComponent(suite)}`,
    ),
  startFlow: (suite: string, layers?: string[], reviewSampling?: ReviewSamplingConfig) =>
    request<{ runId: string }>('/api/evals/v1/agent-flow', {
      method: 'POST',
      body: JSON.stringify({ suite, layers, reviewSampling }),
    }),
  datasetCases: (datasetId: string, version?: number) =>
    request<{ cases: CaseDefinition[] }>(
      `/api/evals/v1/datasets/${encodeURIComponent(datasetId)}/cases${version === undefined ? '' : `?version=${version}`}`,
    ),
  runCases: (runId: string) =>
    request<{ cases: CaseDefinition[] }>(`/api/evals/v1/runs/${encodeURIComponent(runId)}/cases`),
  datasets: () => request<{ datasets: DatasetInfo[] }>('/api/evals/v1/datasets'),
  plannerRoutes: () => request<{ routes: PlannerRoutePublic[] }>('/api/evals/v1/planner/routes'),
  suggestBaseline: (runId?: string) =>
    request<{
      candidateRunId: string | null
      baselineRunId: string | null
      reason: string | null
      options: RunListItem[]
    }>(`/api/evals/v1/suggest-baseline${runId ? `?run_id=${encodeURIComponent(runId)}` : ''}`),
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
  suggestPair: (runId?: string) =>
    request<PairSuggestion>(
      `/api/evals/v1/suggest-pair${runId ? `?run_id=${encodeURIComponent(runId)}` : ''}`,
    ),
  validatePair: (baselineRunId: string, candidateRunId: string) =>
    request<ValidatedPair>('/api/evals/v1/validate-pair', {
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
