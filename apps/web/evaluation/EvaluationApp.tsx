import { RunDiagnostics } from './RunDiagnostics.js'
import { EvaluationTable } from './EvaluationTable.js'
import {
  defaultReviewSampling,
  ReviewSamplingSettings,
  reviewReason,
} from './ReviewSamplingSettings.js'
/** FRAYUNE 评测工作台(§15):数据集/新建/运行详情/审核/比较/成本与压缩/运营。

交互纪律:
- 创建 run 立即返回(后台调度),列表与详情轮询刷新;刷新页面后可继续追踪。
- 长任务不阻塞页面:所有按钮异步反馈,错误/空态/加载态显式呈现。
- 未配置的能力(live 密钥、Langfuse)如实显示"未配置",不显示已接通。
 */

import { ComparisonWorkspace } from './ComparisonWorkspace.js'
import { RunLaunchStatus, useRunLaunch } from './RunLaunchStatus.js'
import {
  CaseIdentity,
  RunCaseIdentity,
  RunCheckLabel,
  useRunDefinitions,
  AgentSourceCases,
  harnessLayers,
  DatasetCases,
  RunCaseDefinition,
} from './CaseBrowser.js'
import { checkLabel, failureReason, humanValue } from './caseCopy.js'
import { FlowOverview } from './FlowOverview.js'
import { PlannerMetrics } from './PlannerMetrics.js'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { QueryClient, QueryClientProvider, useQueryClient } from '@tanstack/react-query'
import { ACTIVE_POLL_MS, LIST_POLL_MS, OPS_POLL_MS, useEvalQuery } from './queries.js'
import {
  api,
  ApiError,
  type DatasetInfo,
  type GradeView,
  type PlannerRoutePublic,
  type PairRunPrefill,
  type ReviewTaskView,
  type RunDetail,
  type TrialView,
} from './api.js'

type Tab =
  'datasets' | 'overview' | 'new' | 'runs' | 'reviews' | 'compare' | 'cost' | 'ops' | 'diagnostics'

const TABS: { id: Tab; label: string }[] = [
  { id: 'overview', label: '评测总览' },
  { id: 'datasets', label: '数据集' },
  { id: 'new', label: '新建评测' },
  { id: 'runs', label: '运行详情' },
  { id: 'reviews', label: '人工审核' },
  { id: 'compare', label: '批次比较' },
  { id: 'cost', label: '成本与压缩' },
  { id: 'ops', label: '运营状态' },
  { id: 'diagnostics', label: 'Run 排障' },
]

const ACTIVE_STATUSES = new Set(['queued', 'running', 'stopping'])

export const STATUS_LABELS: Record<string, string> = {
  pending: '等待执行',
  timed_out: '已超时',
  queued: '排队中',
  running: '运行中',
  stopping: '取消中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
  interrupted: '已中断(待恢复)',
  budget_exhausted: '预算中止',
  draft: '草稿',
}

function percent(value: number | null | undefined): string {
  return value === null || value === undefined ? 'N/A' : `${(value * 100).toFixed(1)}%`
}

function money(value: number | null | undefined, currency = 'CNY'): string {
  return value === null || value === undefined ? 'N/A' : `${value.toFixed(4)} ${currency}`
}

function statusLabel(status: string | null | undefined): string {
  if (!status) {
    return '—'
  }
  return STATUS_LABELS[status] ?? status
}

function ErrorNote({ message }: { message: string | null }) {
  if (!message) {
    return null
  }
  return <div className="eval-error">{message}</div>
}

function EmptyNote({ text }: { text: string }) {
  return <div className="eval-empty">{text}</div>
}

function OverviewTab({ onOpenRun }: { onOpenRun: (runId: string) => void }) {
  const [datasetSelection, setDatasetSelection] = useState<{ id: string; version: number } | null>(
    null,
  )
  const runsQuery = useEvalQuery(['eval-runs'], () => api.runs(), LIST_POLL_MS)
  const datasetsQuery = useEvalQuery(['eval-datasets'], () => api.datasets())
  const runs = runsQuery.data?.runs ?? null
  const datasets = datasetsQuery.data?.datasets ?? null
  const error = runsQuery.error?.message ?? datasetsQuery.error?.message ?? null

  if (error && runs === null && datasets === null) {
    return <ErrorNote message={error} />
  }
  return (
    <section>
      <h2>数据集</h2>
      <AgentSourceCases />
      <h3>数据集(冻结版本)</h3>
      {datasetsQuery.isPending ? (
        <EmptyNote text="加载中…" />
      ) : datasets === null || datasets.length === 0 ? (
        <EmptyNote text="暂无数据集" />
      ) : (
        <EvaluationTable className="eval-table">
          <thead>
            <tr>
              <th>数据集</th>
              <th>版本</th>
              <th>划分</th>
              <th>用例数</th>
              <th>快照哈希</th>
            </tr>
          </thead>
          <tbody>
            {datasets.map((dataset) =>
              dataset.versions.map((version) => (
                <tr key={`${dataset.datasetId}@${version.version}`}>
                  <td>
                    <button
                      type="button"
                      className="eval-link"
                      onClick={() =>
                        setDatasetSelection({ id: dataset.datasetId, version: version.version })
                      }
                    >
                      {dataset.datasetId}
                    </button>
                  </td>
                  <td>v{version.version}</td>
                  <td>{version.split}</td>
                  <td>{version.caseCount}</td>
                  <td>
                    <code>{version.contentHash}…</code>
                  </td>
                </tr>
              )),
            )}
          </tbody>
        </EvaluationTable>
      )}
      {datasetSelection && (
        <DatasetCases
          key={`${datasetSelection.id}@${datasetSelection.version}`}
          datasetId={datasetSelection.id}
          version={datasetSelection.version}
        />
      )}
      <h3>运行记录(每 4 秒刷新)</h3>
      {runsQuery.isPending ? (
        <EmptyNote text="加载中…" />
      ) : runs === null || runs.length === 0 ? (
        <EmptyNote text="尚无评测运行。" />
      ) : (
        <EvaluationTable className="eval-table">
          <thead>
            <tr>
              <th>runId</th>
              <th>模式</th>
              <th>Provider</th>
              <th>状态</th>
              <th>计划数</th>
              <th>说明</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.runId}>
                <td>
                  <code>{run.runId}</code>
                </td>
                <td>{run.mode}</td>
                <td>{run.provider}</td>
                <td>{statusLabel(run.status)}</td>
                <td>{run.planCount}</td>
                <td>{run.purpose || '—'}</td>
                <td>
                  <button type="button" onClick={() => onOpenRun(run.runId)}>
                    查看详情
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </EvaluationTable>
      )}
    </section>
  )
}

function NewRunTab({
  onCreated,
  onReviews,
  prefill,
}: {
  onCreated: (runId: string) => void
  onReviews: (runId: string) => void
  prefill?: PairRunPrefill | null
}) {
  const launch = useRunLaunch()
  const [reviewSampling, setReviewSampling] = useState(defaultReviewSampling)
  const [datasets, setDatasets] = useState<DatasetInfo[]>([])
  const [datasetId, setDatasetId] = useState('')
  const [versionOverride, setVersionOverride] = useState<number | null>(null)
  const [agentLayers, setAgentLayers] = useState<string[]>(harnessLayers)
  const [runId, setRunId] = useState('')
  const [repetitions, setRepetitions] = useState(1)
  const [purpose, setPurpose] = useState('')
  const [replaySource, setReplaySource] = useState('')
  const [recordingId, setRecordingId] = useState('')
  const [liveMode, setLiveMode] = useState(false)
  const [maxCost, setMaxCost] = useState('0')
  const [genPrice, setGenPrice] = useState('')
  const [judgePrice, setJudgePrice] = useState('')
  const [judgeModel, setJudgeModel] = useState('')
  const [plannerDataset, setPlannerDataset] = useState(false)
  const [plannerMode, setPlannerMode] = useState<'rule' | 'llm'>('rule')
  const [plannerRouteId, setPlannerRouteId] = useState('')
  const [plannerRoutes, setPlannerRoutes] = useState<PlannerRoutePublic[]>([])
  const [plannerRoutesNote, setPlannerRoutesNote] = useState<string | null>(null)
  const [compression, setCompression] = useState(false)
  const [useCache, setUseCache] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    void api
      .datasets()
      .then((data) => {
        setDatasets(data.datasets)
        if (data.datasets.length > 0) {
          setDatasetId(data.datasets[0].datasetId)
        }
      })
      .catch((exc: unknown) => setError(exc instanceof Error ? exc.message : '加载失败'))
  }, [])

  // 配对重跑引导:同数据集 + 反向压缩开关 + live 配置一次填好,用户只需确认费用参数。
  const appliedPrefill = useRef(false)
  useEffect(() => {
    if (!prefill || appliedPrefill.current || datasets.length === 0) {
      return
    }
    appliedPrefill.current = true
    if (prefill.datasetId && datasets.some((item) => item.datasetId === prefill.datasetId)) {
      setDatasetId(prefill.datasetId)
    }
    setVersionOverride(prefill.version ?? null)
    setCompression(Boolean(prefill.compression?.enabled))
    setPurpose(prefill.purpose ?? '')
    if (prefill.mode === 'live') {
      setLiveMode(true)
      const budget = prefill.budget as
        { maxCost?: number; prices?: Record<string, number> } | undefined
      if (budget?.maxCost !== undefined && budget.maxCost !== null) {
        setMaxCost(String(budget.maxCost))
      }
      if (budget?.prices?.generation !== undefined) {
        setGenPrice(String(budget.prices.generation))
      }
      if (budget?.prices?.judge !== undefined) {
        setJudgePrice(String(budget.prices.judge))
      }
      const judgeConfig = prefill.judgeConfig as { model?: string } | undefined
      if (judgeConfig?.model) {
        setJudgeModel(judgeConfig.model)
      } else if (prefill.judge === 'vlm') {
        setJudgeModel('qwen-vl-max-latest')
      }
    }
  }, [prefill, datasets])

  const selectedVersion =
    versionOverride ??
    datasets
      .find((dataset) => dataset.datasetId === datasetId)
      ?.versions.reduce<number | undefined>(
        (latest, entry) => Math.max(latest ?? 0, entry.version),
        undefined,
      )

  useEffect(() => {
    if (!datasetId || datasetId === '__agent_source__') {
      setPlannerDataset(false)
      return
    }
    let cancelled = false
    void api
      .datasetCases(datasetId, selectedVersion)
      .then((data) => {
        if (!cancelled) {
          setPlannerDataset(data.cases.some((item) => item.taskType === 'planner'))
        }
      })
      .catch(() => {
        if (!cancelled) {
          setPlannerDataset(false)
        }
      })
    return () => {
      cancelled = true
    }
  }, [datasetId, selectedVersion])

  useEffect(() => {
    if (!plannerDataset) {
      setPlannerRoutes([])
      setPlannerRoutesNote(null)
      setPlannerMode('rule')
      setPlannerRouteId('')
      return
    }
    let cancelled = false
    void api
      .plannerRoutes()
      .then((data) => {
        if (cancelled) {
          return
        }
        setPlannerRoutes(data.routes)
        setPlannerRouteId(data.routes[0]?.id ?? '')
        setPlannerRoutesNote(
          data.routes.length === 0
            ? '未配置部署路由。LLM 规划评测不能启动，也不会记为已接通。'
            : null,
        )
      })
      .catch((exc: unknown) => {
        if (cancelled) {
          return
        }
        setPlannerRoutes([])
        setPlannerRouteId('')
        setPlannerRoutesNote(exc instanceof Error ? exc.message : '部署路由不可用')
      })
    return () => {
      cancelled = true
    }
  }, [plannerDataset])

  const submit = useCallback(() => {
    setBusy(true)
    setError(null)
    const plannerSandbox: Record<string, string> =
      plannerMode === 'llm' ? { plannerMode: 'llm', plannerRouteId } : { plannerMode: 'rule' }
    const body: Parameters<typeof api.createRun>[0] = plannerDataset
      ? {
          runId,
          datasetId,
          version: selectedVersion,
          repetitions,
          purpose,
          mode: 'mock',
          provider: 'mock',
          judge: 'stub',
          budget: {
            currency: 'CNY',
            maxCost: Number(maxCost) || 0,
            prices: {},
          },
          sandboxConfig: plannerSandbox,
          compression: { enabled: compression },
          useGradeCache: useCache,
        }
      : liveMode
        ? {
            runId,
            datasetId,
            version: selectedVersion,
            repetitions,
            purpose,
            mode: 'live',
            provider: 'cloud',
            judge: judgeModel ? 'vlm' : 'stub',
            judgeConfig: judgeModel ? { model: judgeModel } : undefined,
            budget: {
              currency: 'CNY',
              maxCost: Number(maxCost) || 0,
              prices: {
                generation: Number(genPrice) || 0,
                translate: 0,
                poll: 0,
                judge: Number(judgePrice) || 0,
              },
            },
            compression: { enabled: compression },
            useGradeCache: useCache,
          }
        : {
            runId,
            datasetId,
            version: selectedVersion,
            repetitions,
            purpose,
            mode: 'mock',
            // mock 也如实记录压缩开关:配对校验按 not_compression_pair/missing_cost 精确报因,
            // 不会把「忘了开压缩」误报成「快照不一致」。
            compression: { enabled: compression },
            useGradeCache: useCache,
          }
    body.reviewSampling = reviewSampling
    api
      .createRun(body)
      .then((result) => {
        launch.recordRun(result.runId)
      })
      .catch((exc: unknown) => setError(exc instanceof ApiError ? exc.message : String(exc)))
      .finally(() => setBusy(false))
  }, [
    reviewSampling,
    plannerDataset,
    plannerMode,
    plannerRouteId,
    liveMode,
    runId,
    datasetId,
    selectedVersion,
    repetitions,
    purpose,
    maxCost,
    genPrice,
    judgePrice,
    judgeModel,
    compression,
    useCache,
    launch,
  ])

  const submitReplay = useCallback(() => {
    setBusy(true)
    setError(null)
    api
      .createRun({
        runId,
        mode: 'replay',
        sourceRunId: replaySource,
        recordingId: recordingId || undefined,
        purpose: purpose || '严格回放',
      })
      .then((result) => {
        launch.recordRun(result.runId)
      })
      .catch((exc: unknown) => setError(exc instanceof ApiError ? exc.message : String(exc)))
      .finally(() => setBusy(false))
  }, [runId, replaySource, recordingId, purpose, launch])

  if (datasetId === '__agent_source__') {
    return (
      <section className="eval-new-page">
        <h2>新建评测</h2>
        <ErrorNote message={error} />
        <RunLaunchStatus launch={launch} onOpenRun={onCreated} onReviews={onReviews} />
        <label>
          数据集
          <select
            value={datasetId}
            disabled={busy || launch.active}
            onChange={(event) => setDatasetId(event.target.value)}
          >
            <option value="__agent_source__">Agent 流程 · 源题库（L0～L5、L7）</option>
            {datasets.map((dataset) => (
              <option key={dataset.datasetId} value={dataset.datasetId}>
                {dataset.datasetId}
              </option>
            ))}
          </select>
        </label>
        <fieldset disabled={busy || launch.active}>
          <AgentSourceCases selection={agentLayers} onSelection={setAgentLayers} />
        </fieldset>
        <ReviewSamplingSettings value={reviewSampling} onChange={setReviewSampling} />
        <p>运行编号自动生成，每题执行一次，使用固定模拟模型与流程规则判分。</p>
        <button
          className="eval-primary"
          disabled={
            busy ||
            launch.active ||
            !agentLayers.length ||
            reviewSampling.grayMin > reviewSampling.grayMax
          }
          onClick={() => {
            setBusy(true)
            setError(null)
            void api
              .startFlow('agent_flow', agentLayers, reviewSampling)
              .then((result) => launch.recordRun(result.runId))
              .catch((exc: unknown) => setError(exc instanceof Error ? exc.message : String(exc)))
              .finally(() => setBusy(false))
          }}
        >
          {busy || launch.active ? '本次评测进行中…' : '启动所选 Agent 层级'}
        </button>
      </section>
    )
  }

  return (
    <section className="eval-new-page">
      <h2>新建评测</h2>
      <ErrorNote message={error} />
      <RunLaunchStatus launch={launch} onOpenRun={onCreated} onReviews={onReviews} />
      <div className="eval-create-layout">
        <div className="eval-config-card">
          <h3>配置评测</h3>
          <div className="eval-field-grid">
            <label>
              数据集
              <select
                value={datasetId}
                onChange={(event) => {
                  setDatasetId(event.target.value)
                  setVersionOverride(null)
                }}
              >
                <option value="__agent_source__">Agent 流程 · 源题库（L0～L5、L7）</option>
                {datasets.map((dataset) => (
                  <option key={dataset.datasetId} value={dataset.datasetId}>
                    {dataset.datasetId}(最新 v
                    {Math.max(...dataset.versions.map((entry) => entry.version))})
                  </option>
                ))}
              </select>
            </label>
            {plannerDataset && (
              <>
                <label>
                  规划器
                  <select
                    aria-label="规划器"
                    value={plannerMode}
                    onChange={(event) => {
                      const next = event.target.value === 'llm' ? 'llm' : 'rule'
                      setPlannerMode(next)
                    }}
                  >
                    <option value="rule">Rule 基线</option>
                    <option value="llm">LLM 部署路由</option>
                  </select>
                </label>
                {plannerMode === 'llm' && (
                  <label>
                    部署 route
                    <select
                      aria-label="部署 route"
                      value={plannerRouteId}
                      onChange={(event) => setPlannerRouteId(event.target.value)}
                      disabled={plannerRoutes.length === 0}
                    >
                      {plannerRoutes.length === 0 ? (
                        <option value="">未配置</option>
                      ) : (
                        plannerRoutes.map((route) => (
                          <option key={route.id} value={route.id}>
                            {route.label}（{route.id} · {route.provider}/{route.model}）
                          </option>
                        ))
                      )}
                    </select>
                  </label>
                )}
                {plannerRoutesNote && <p className="eval-prefill-note">{plannerRoutesNote}</p>}
                <p className="eval-prefill-note">
                  Rule 基线只跑 RulePlanner。LLM 使用已部署的 route id，页面不提交密钥或 URL。
                </p>
              </>
            )}
            {versionOverride !== null && (
              <p className="eval-prefill-note">
                已锁定数据集版本 v{versionOverride}（与配对运行的冻结快照一致）。
              </p>
            )}
            <label>
              运行 ID
              <input
                value={runId}
                onChange={(event) => setRunId(event.target.value)}
                placeholder="web-run-1"
              />
            </label>
            <label>
              重复次数
              <input
                type="number"
                min={1}
                max={10}
                value={repetitions}
                onChange={(event) => setRepetitions(Number(event.target.value))}
              />
            </label>
            <label>
              目的说明
              <input value={purpose} onChange={(event) => setPurpose(event.target.value)} />
            </label>
          </div>
          {!plannerDataset && (
            <label className="eval-checkbox">
              <input
                type="checkbox"
                checked={liveMode}
                onChange={(e) => setLiveMode(e.target.checked)}
              />
              live 模式(真实调用,产生费用)
            </label>
          )}
          {plannerDataset && (
            <div className="eval-live-config">
              <label>
                预算上限(CNY)
                <input
                  aria-label="预算上限"
                  value={maxCost}
                  onChange={(e) => setMaxCost(e.target.value)}
                  placeholder="5.0"
                />
              </label>
            </div>
          )}
          {!plannerDataset && liveMode && (
            <div className="eval-live-config">
              <label>
                预算上限(CNY,必填正数)
                <input
                  value={maxCost}
                  onChange={(e) => setMaxCost(e.target.value)}
                  placeholder="5.0"
                />
              </label>
              <label>
                生成单价/次(CNY)
                <input
                  value={genPrice}
                  onChange={(e) => setGenPrice(e.target.value)}
                  placeholder="0.5"
                />
              </label>
              <label>
                裁判单价/次(CNY,可选)
                <input
                  value={judgePrice}
                  onChange={(e) => setJudgePrice(e.target.value)}
                  placeholder="0"
                />
              </label>
              <label>
                裁判模型
                <input
                  value={judgeModel}
                  onChange={(e) => setJudgeModel(e.target.value)}
                  placeholder="qwen-vl-max-latest"
                />
              </label>
            </div>
          )}
          <div className="eval-option-group">
            <label className="eval-checkbox">
              <input
                type="checkbox"
                checked={useCache}
                onChange={(e) => setUseCache(e.target.checked)}
              />
              启用判分缓存
            </label>
            <label className="eval-checkbox">
              <input
                type="checkbox"
                checked={compression}
                onChange={(e) => setCompression(e.target.checked)}
              />
              提示词压缩实验（本次运行 compression: {compression ? 'on' : 'off'}）
            </label>
          </div>
          <p className="eval-prefill-note">
            成本对比需要一方关闭压缩、一方开启压缩，且两次都是带预算记账的 live 运行；mock
            运行不产生费用与 token。
          </p>
          {datasetId && (
            <DatasetCases
              key={`${datasetId}@${selectedVersion}`}
              datasetId={datasetId}
              version={selectedVersion}
              preview
            />
          )}
          <ReviewSamplingSettings value={reviewSampling} onChange={setReviewSampling} />
          <div className="eval-submit-bar">
            <button
              className="eval-primary"
              type="button"
              disabled={
                busy ||
                launch.active ||
                !runId ||
                (!liveMode && !datasetId) ||
                (plannerDataset &&
                  plannerMode === 'llm' &&
                  (plannerRoutes.length === 0 || !plannerRouteId)) ||
                reviewSampling.grayMin > reviewSampling.grayMax
              }
              onClick={submit}
            >
              {busy
                ? '正在启动…'
                : launch.active
                  ? '评测进行中…'
                  : plannerDataset
                    ? plannerMode === 'llm'
                      ? '预检并启动 LLM 规划评测'
                      : '预检并启动 Rule 基线'
                    : liveMode
                      ? '预检并启动 live 运行'
                      : '预检并启动 Mock 运行'}
            </button>
          </div>
        </div>
        <details className="eval-replay-card">
          <summary>严格回放已有运行</summary>
          <label>
            源 runId
            <input value={replaySource} onChange={(event) => setReplaySource(event.target.value)} />
          </label>
          <label>
            录制 ID
            <input value={recordingId} onChange={(event) => setRecordingId(event.target.value)} />
          </label>
          <button
            type="button"
            disabled={busy || launch.active || !runId || !replaySource}
            onClick={submitReplay}
          >
            启动严格回放
          </button>
        </details>
      </div>
    </section>
  )
}

function StepsPanel({ runId, trialId }: { runId: string; trialId: string }) {
  const eventsQuery = useEvalQuery(['eval-events', runId], () => api.events(runId))
  if (eventsQuery.isPending) {
    return <EmptyNote text="加载步骤…" />
  }
  const events = eventsQuery.isError
    ? []
    : eventsQuery.data.events.filter(
        (item) => item.trialId === trialId || item.payload?.trialId === trialId,
      )
  if (events.length === 0) {
    return <EmptyNote text="该 trial 没有步骤事件(legacy 导入无过程记录)。" />
  }
  return (
    <ol className="eval-steps">
      {events.map((event) => (
        <li key={event.eventId}>
          <code>{event.type}</code>
          {event.stepId ? ` ${event.stepId}` : ''}{' '}
          <span className="eval-muted">{event.timestamp}</span>
          {Object.keys(event.payload).length > 0 && (
            <pre className="eval-payload">{JSON.stringify(event.payload, null, 2)}</pre>
          )}
        </li>
      ))}
    </ol>
  )
}

function ReplayEvidencePanel({ detail }: { detail: RunDetail }) {
  const replay = detail.extra.replay
  if (!replay) {
    return null
  }
  return (
    <div className="eval-replay-evidence">
      <h4>回放证据(协议 v{replay.matchProtocolVersion})</h4>
      <p>
        匹配 {replay.matched};未消费(计划内){replay.unconsumedInPlan.length}; 新增外部调用{' '}
        {replay.newExternalCalls}(实际观测,不硬编码 0); 校验{' '}
        <strong>{replay.verification === 'passed' ? '通过' : '失败'}</strong>
      </p>
      {replay.newExternalCallDetail.length > 0 && (
        <ul>
          {replay.newExternalCallDetail.map((call, index) => (
            <li key={index}>
              <code>{call.method}</code> {call.url}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function BudgetPanel({ runId }: { runId: string }) {
  const budgetQuery = useEvalQuery(['eval-budget', runId], () => api.budget(runId))
  if (budgetQuery.isPending) {
    return <EmptyNote text="加载预算账本…" />
  }
  if (budgetQuery.isError) {
    return <ErrorNote message={budgetQuery.error.message} />
  }
  const budget = budgetQuery.data
  return (
    <div className="eval-budget">
      <h4>预算账本(预留 ≠ 实际费用)</h4>
      <EvaluationTable className="eval-table">
        <tbody>
          <tr>
            <th>上限</th>
            <td>{budget.maxCost === null ? '未限定' : money(budget.maxCost, budget.currency)}</td>
            <th>已结算(账单口径)</th>
            <td>{money(budget.settledBilled, budget.currency)}</td>
          </tr>
          <tr>
            <th>已结算(估算口径)</th>
            <td>{money(budget.settledEstimated, budget.currency)}</td>
            <th>未结预留</th>
            <td>{money(budget.outstandingReserved, budget.currency)}</td>
          </tr>
          <tr>
            <th>未知费用调用</th>
            <td>{budget.unknownCount}</td>
            <th>总调用 / 次数上限</th>
            <td>
              {budget.totalCalls} / {budget.maxExternalCalls ?? '未限定'}
            </td>
          </tr>
        </tbody>
      </EvaluationTable>
      {budget.project && (
        <p className="eval-muted">
          项目总预算:已结算 {money(budget.project.settled)},未结预留{' '}
          {money(budget.project.outstandingReserved)},未知 {budget.project.unknownCount}
        </p>
      )}
      {budget.reservations && budget.reservations.length > 0 && (
        <EvaluationTable className="eval-table">
          <thead>
            <tr>
              <th>调用点</th>
              <th>状态</th>
              <th>预留</th>
              <th>结算</th>
              <th>口径</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            {budget.reservations.map((reservation) => (
              <tr key={reservation.reservationId}>
                <td>{reservation.callSite}</td>
                <td>{reservation.status}</td>
                <td>{money(reservation.reservedAmount, budget.currency)}</td>
                <td>{money(reservation.settledAmount, budget.currency)}</td>
                <td>{reservation.settledBasis ?? '—'}</td>
                <td>{reservation.note ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </EvaluationTable>
      )}
    </div>
  )
}

/** 入口 A:运行详情一键配对——找得到对手直接对比,找不到给重跑引导(§12.3)。 */
function RunPairingPanel({
  runId,
  compressed,
  onComparePair,
  onCreatePairRun,
  onOpenCost,
}: {
  runId: string
  compressed: boolean
  onComparePair: (pair: { baseline: string; candidate: string }) => void
  onCreatePairRun: (prefill: PairRunPrefill) => void
  onOpenCost: (runId: string) => void
}) {
  const pairQuery = useEvalQuery(['eval-pair', runId], () => api.suggestPair(runId))
  if (pairQuery.isPending) {
    return <p className="run-pairing">正在寻找可对比的配对运行…</p>
  }
  if (pairQuery.error) {
    return <ErrorNote message={`配对推荐读取失败：${pairQuery.error.message}`} />
  }
  const suggestion = pairQuery.data
  if (!suggestion) {
    return null
  }
  if (suggestion.pairRunId && suggestion.currentRunId) {
    const baseline =
      suggestion.currentRole === 'compressed' ? suggestion.pairRunId : suggestion.currentRunId
    const candidate =
      suggestion.currentRole === 'compressed' ? suggestion.currentRunId : suggestion.pairRunId
    return (
      <div className="run-pairing run-pairing-ready">
        <button
          type="button"
          className="eval-primary"
          onClick={() => onComparePair({ baseline, candidate })}
        >
          {compressed
            ? `与未压缩基线 ${suggestion.pairRunId} 对比成本`
            : `与压缩运行 ${suggestion.pairRunId} 对比成本`}
        </button>
        <span>同数据集、快照一致且双方已记账，点击直接生成费用 / Token / 通过率报告。</span>
      </div>
    )
  }
  return (
    <div className="run-pairing run-pairing-missing">
      <strong>{compressed ? '未找到可对比的未压缩基线' : '未找到可对比的压缩运行'}</strong>
      <ul>
        {suggestion.reasons.map((item, index) => (
          <li key={`${item.code}:${index}`}>
            {item.message} <small>({item.code})</small>
          </li>
        ))}
      </ul>
      {suggestion.guidance && (
        <div className="pair-guidance-action">
          <p>{suggestion.guidance.message}</p>
          <button
            type="button"
            className="eval-primary"
            onClick={() => onCreatePairRun(suggestion.guidance!.prefill)}
          >
            一键新建配对运行（同数据集
            {suggestion.guidance.prefill.compression.enabled ? '、开启压缩' : '、关闭压缩'}）
          </button>
        </div>
      )}
      <button type="button" onClick={() => onOpenCost(runId)}>
        查看成本与压缩页
      </button>
    </div>
  )
}

function RunDetailPanel({
  runId,
  onCompare,
  onComparePair,
  onCreatePairRun,
}: {
  runId: string
  onCompare: (id: string) => void
  onComparePair: (pair: { baseline: string; candidate: string }) => void
  onCreatePairRun: (prefill: PairRunPrefill) => void
}) {
  const resultLabel = (value: unknown): string => {
    const labels: Record<string, string> = {
      true: '是',
      false: '否',
      sse_terminal: '流结束事件',
      outcome: '执行结果',
      route: '任务路由',
      must_ask_user: '用户确认',
      success_artifacts: '生成成功数',
      draft_cleared: '续跑草稿已清除',
      tool_handoff_to_planner: '转交策划工具',
      interrupt_approval: '积分确认',
      interrupted: '等待确认',
      handoff_planner: '转交策划',
      host_only: '直接生成',
      completed: '已完成',
      stream_complete: '正常结束',
      cancelled: '已取消',
    }
    return value == null ? '—' : (labels[String(value)] ?? humanValue(value))
  }
  const [showDefinition, setShowDefinition] = useState(false)
  const [selected, setSelected] = useState<TrialView | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  useEffect(() => {
    if (selected) {
      document.getElementById('run-selected-result')?.scrollIntoView({ block: 'start' })
    }
  }, [selected])
  const [showBudget, setShowBudget] = useState(false)
  const queryClient = useQueryClient()

  // 运行中的 run 每 2 秒轮询;终态停止轮询(长任务不阻塞页面,刷新后可恢复追踪)。
  const detailQuery = useEvalQuery(
    ['eval-run', runId],
    () => api.run(runId),
    (query) => {
      const current = query.state.data as RunDetail | undefined
      return current && ACTIVE_STATUSES.has(current.status) ? ACTIVE_POLL_MS : false
    },
  )
  const trialsQuery = useEvalQuery(['eval-trials', runId], () => api.trials(runId))
  const definitions = useRunDefinitions(runId)
  const [caseSearch, setCaseSearch] = useState('')
  const [resultFilter, setResultFilter] = useState('all')
  const gatesQuery = useEvalQuery(['eval-gates', runId], () => api.gates(runId))

  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['eval-run', runId] })
    void queryClient.invalidateQueries({ queryKey: ['eval-trials', runId] })
    void queryClient.invalidateQueries({ queryKey: ['eval-gates', runId] })
  }, [queryClient, runId])

  const computeGate = useCallback(() => {
    api
      .gate(runId)
      .then(() => refresh())
      .catch((exc: unknown) => setActionError(exc instanceof Error ? exc.message : '门禁计算失败'))
  }, [runId, refresh])

  const createReport = useCallback(() => {
    api
      .createReport(runId)
      .then(() => refresh())
      .catch((exc: unknown) => setActionError(exc instanceof Error ? exc.message : '报告生成失败'))
  }, [runId, refresh])

  const cancel = useCallback(() => {
    api
      .cancelRun(runId)
      .then(() => refresh())
      .catch((exc: unknown) => setActionError(exc instanceof Error ? exc.message : '取消失败'))
  }, [runId, refresh])

  if (detailQuery.error && !detailQuery.data) {
    return <ErrorNote message={detailQuery.error.message} />
  }
  if (detailQuery.isPending || !detailQuery.data) {
    return <EmptyNote text="加载中…" />
  }
  const detail = detailQuery.data
  const trials = trialsQuery.data?.trials ?? []
  const gates = gatesQuery.data?.gates ?? []
  const metrics = detail.metrics
  const cancellable = ACTIVE_STATUSES.has(detail.status)
  const caseById = new Map((definitions.data?.cases ?? []).map((item) => [item.caseId, item]))
  const visibleTrials = trials.filter(
    (trial) =>
      (resultFilter === 'all' ||
        (resultFilter === 'pass'
          ? trial.qualityVerdict === 'pass'
          : resultFilter === 'fail'
            ? trial.qualityVerdict === 'fail' ||
              trial.status === 'failed' ||
              trial.status === 'timed_out'
            : trial.qualityVerdict !== 'pass' && trial.qualityVerdict !== 'fail')) &&
      `${trial.caseId} ${caseById.get(trial.caseId)?.title ?? ''} ${caseById.get(trial.caseId)?.input.prompt ?? ''}`
        .toLowerCase()
        .includes(caseSearch.toLowerCase()),
  )
  return (
    <div className="run-workspace">
      {detail.status === 'completed' &&
        (detail.flow ? (
          <p className="run-pairing">Agent 流程 run 由类型化规则判定，不走成本与压缩对比。</p>
        ) : (
          <RunPairingPanel
            runId={runId}
            compressed={Boolean(detail.manifest.compression?.enabled)}
            onComparePair={onComparePair}
            onCreatePairRun={onCreatePairRun}
            onOpenCost={onCompare}
          />
        ))}
      <h3 className="run-identity">
        {detail.manifest.purpose || '评测结果'}
        <small>
          运行编号：{runId} ·{' '}
          {detail.manifest.mode === 'mock'
            ? '模拟运行'
            : detail.manifest.mode === 'live'
              ? '真实调用'
              : '回放'}{' '}
          · {statusLabel(detail.status)}
        </small>
      </h3>
      <div className="run-meta">
        <span>
          {detail.manifest.dataset.datasetId} · v{detail.manifest.dataset.version}
        </span>
        <span>
          完成 {detail.progress.completed}/{detail.progress.total}
        </span>
        <span>运行中 {detail.progress.running}</span>
        <span>排队 {detail.progress.pending}</span>
        <span>失败 {detail.progress.failed}</span>
        <span>取消 {detail.progress.cancelled}</span>
      </div>
      {detail.extra.error && <ErrorNote message={detail.extra.error} />}
      <details className="run-config">
        <summary>运行配置</summary>
        <dl>
          <dt>数据集快照</dt>
          <dd>{detail.manifest.dataset.contentHash}</dd>
          <dt>计划用例</dt>
          <dd>{detail.denominators.plan}</dd>
          <dt>压缩实验</dt>
          <dd>compression: {detail.manifest.compression.enabled ? 'on' : 'off'}</dd>
          {detail.manifest.parentRunId && (
            <>
              <dt>回放来源</dt>
              <dd>{detail.manifest.parentRunId}</dd>
            </>
          )}
        </dl>
      </details>
      {detail.flow ? (
        <div className="flow-panel eval-result-panel">
          <div className="eval-result-summary">
            <div>
              <span>流程通过率</span>
              <strong>{percent(detail.flow.passRate)}</strong>
            </div>
            <div>
              <span>通过用例</span>
              <strong>
                {detail.denominators.pass}
                <em> / {detail.denominators.plan}</em>
              </strong>
            </div>
            <div>
              <span>执行状态</span>
              <strong className="eval-result-state">{statusLabel(detail.status)}</strong>
            </div>
          </div>
          {Object.keys(detail.flow.layers).length > 1 && (
            <div className="eval-layer-results">
              {Object.entries(detail.flow.layers)
                .sort(([a], [b]) => a.localeCompare(b))
                .map(([layer, result]) => (
                  <div className="eval-layer-result" key={layer}>
                    <div>
                      <b>{layer}</b>
                      <span>
                        {
                          (
                            {
                              L0: '协议',
                              L1: '路由',
                              L2: '计划',
                              L3: '中断',
                              L4: '续跑',
                              L5: '队列',
                              L7: '反思',
                            } as Record<string, string>
                          )[layer]
                        }
                      </span>
                    </div>
                    <strong>
                      {result.pass}
                      <span> / {result.plan}</span>
                    </strong>
                    <progress
                      value={result.pass}
                      max={Math.max(result.plan, 1)}
                      aria-label={`${layer} 通过进度`}
                    />
                    <span
                      className={
                        result.pass === result.plan ? 'eval-status-pass' : 'eval-status-pending'
                      }
                    >
                      {result.pass === result.plan ? '全部通过' : `${result.fail} 条失败或待完成`}
                    </span>
                  </div>
                ))}
            </div>
          )}
          {detail.flow.failures.map((failure, i) => (
            <div className="failure-row" key={`${failure.caseId}-${i}`}>
              <b>{failure.suite}</b> <code>{failure.caseId}</code>{' '}
              <span className="failure-reason">
                {failureReason(failure.reason)} <small>（原码：{failure.reason}）</small>
              </span>
              <p>Trace：{failure.tracePath ?? '未生成'}</p>
            </div>
          ))}
        </div>
      ) : detail.planner ? (
        <p className="run-pairing">
          Planner 指标单独展示，不进入画质与条件质量排行。
          {detail.planner.synthetic ? '本次为 synthetic，只验证协议。' : ''}
          {detail.planner.llmTested ? '' : 'LLM 未测。'}
        </p>
      ) : (
        <EvaluationTable className="eval-table">
          <tbody>
            <tr>
              <th>执行覆盖率</th>
              <td>{percent(metrics.executionCoverage)}</td>
              <th>生成成功率</th>
              <td>{percent(metrics.generationSuccessRate)}</td>
            </tr>
            <tr>
              <th>判定覆盖率</th>
              <td>{percent(metrics.determinationCoverage)}</td>
              <th>条件质量通过率</th>
              <td>{percent(metrics.conditionalQualityPassRate)}</td>
            </tr>
            <tr>
              <th>计划合格完成率</th>
              <td>{percent(metrics.planCompletionRate)}</td>
              <th>每份合格素材成本</th>
              <td>N/A(见预算账本口径)</td>
            </tr>
          </tbody>
        </EvaluationTable>
      )}
      {detail.planner && <PlannerMetrics planner={detail.planner} />}
      {detail.coverageGaps.length > 0 && (
        <div className="eval-gaps">
          <h4>覆盖缺口(不隐藏、不计为通过)</h4>
          <ul>
            {detail.coverageGaps.map((gap) => (
              <li key={gap}>{gap}</li>
            ))}
          </ul>
        </div>
      )}
      <ReplayEvidencePanel detail={detail} />
      <div className="eval-actions">
        <button type="button" onClick={createReport}>
          生成新版本报告
        </button>
        <button type="button" onClick={computeGate}>
          计算发布门禁
        </button>
        <button type="button" onClick={() => setShowBudget((value) => !value)}>
          {showBudget ? '收起预算账本' : '查看预算账本'}
        </button>
        {cancellable && (
          <button type="button" className="eval-danger" onClick={cancel}>
            取消运行
          </button>
        )}
      </div>
      <ErrorNote message={actionError} />
      {showBudget && <BudgetPanel runId={runId} />}
      {gates.length > 0 && (
        <div>
          <h4>发布门禁判定(新判定不改写旧结论)</h4>
          {gates.map((gate) => (
            <p key={gate.gateId}>
              <span className={`eval-gate-${gate.verdict}`}>{gate.verdict.toUpperCase()}</span>{' '}
              {gate.reasons.length > 0 ? `— ${gate.reasons.join(';')}` : '(无阻断原因)'}
              <span className="eval-muted"> {gate.decidedAt}</span>
            </p>
          ))}
        </div>
      )}
      <div className="run-results-heading">
        <h4 className="eval-results-title">
          用例结果{' '}
          <small>
            {visibleTrials.length} / {trials.length} 条
          </small>
        </h4>
        <div className="run-result-filters">
          <label>
            搜索运行用例
            <input
              value={caseSearch}
              onChange={(event) => setCaseSearch(event.target.value)}
              placeholder="题目名称或用例编号"
            />
          </label>
          <label>
            结果筛选
            <select value={resultFilter} onChange={(event) => setResultFilter(event.target.value)}>
              <option value="all">全部结果</option>
              <option value="fail">失败与超时</option>
              <option value="pass">通过</option>
              <option value="unknown">待执行或未判定</option>
            </select>
          </label>
        </div>
      </div>
      <div className="run-results-scroll">
        <EvaluationTable className="eval-table run-results-table">
          <thead>
            <tr>
              <th>用例</th>
              <th>第几次</th>
              <th>执行</th>
              <th>{detail.flow ? '规则结果' : '质量'}</th>
              <th>{detail.flow ? '规则分' : '加权分'}</th>
              <th>{detail.flow ? '操作' : 'seed(请求/实际)'}</th>
            </tr>
          </thead>
          <tbody>
            {visibleTrials.map((trial) => (
              <tr
                key={trial.trialId}
                className={selected?.trialId === trial.trialId ? 'eval-selected' : ''}
                onClick={() => setSelected(trial)}
              >
                <td>
                  <button
                    className="run-case-name"
                    type="button"
                    onClick={() => setSelected(trial)}
                  >
                    <CaseIdentity item={caseById.get(trial.caseId)} caseId={trial.caseId} />
                  </button>
                  <button
                    type="button"
                    onClick={(event) => {
                      event.stopPropagation()
                      setSelected(trial)
                      setShowDefinition(true)
                    }}
                  >
                    查看用例定义
                  </button>
                </td>
                <td>{trial.repetitionIndex + 1}</td>
                <td>{statusLabel(trial.status)}</td>
                <td>
                  <span
                    className={`eval-status-${trial.qualityVerdict === 'pass' ? 'pass' : trial.qualityVerdict === 'fail' ? 'fail' : 'pending'}`}
                  >
                    {trial.qualityVerdict === 'pass'
                      ? '通过'
                      : trial.qualityVerdict === 'fail'
                        ? '失败'
                        : '未评分'}
                  </span>
                </td>
                <td>{trial.weightedScore === null ? '—' : trial.weightedScore.toFixed(2)}</td>
                <td>
                  {detail.flow ? (
                    <div className="run-row-actions">
                      <button type="button" onClick={() => setSelected(trial)}>
                        查看结果
                      </button>
                      <span className="eval-rule-note">程序判定，无需人工重判</span>
                    </div>
                  ) : (
                    <>
                      {trial.requestedSeed}/{trial.resolvedSeed ?? '?'}
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </EvaluationTable>
      </div>
      {!visibleTrials.length && (
        <p className="eval-empty">没有匹配的运行用例，请调整搜索或结果筛选。</p>
      )}
      {selected && (
        <div id="run-selected-result" className="eval-trial-detail run-selected-result">
          <h4>
            用例详情 <code>{selected.caseId}</code>
          </h4>
          <div className="run-selected-meta">
            <span>执行：{statusLabel(selected.status)}</span>
            <span>
              判定：
              {selected.qualityVerdict === 'pass'
                ? '通过'
                : selected.qualityVerdict === 'fail'
                  ? '失败'
                  : '未判定'}
            </span>
            {selected.tracePath && (
              <a
                target="_blank"
                rel="noreferrer"
                href={`/api/evals/v1/runs/${encodeURIComponent(runId)}/trials/${encodeURIComponent(selected.trialId)}/trace`}
              >
                查看 Trace ↗
              </a>
            )}
          </div>
          <button
            type="button"
            aria-expanded={showDefinition}
            onClick={() => setShowDefinition(!showDefinition)}
          >
            {showDefinition ? '收起用例定义' : '查看用例定义'}
          </button>
          {showDefinition && <RunCaseDefinition runId={runId} trial={selected} />}
          {selected.error && <div className="eval-error">执行错误:{selected.error}</div>}
          <details open={selected.artifactIds.length > 0} className="eval-technical-details">
            <summary>绑定作品（{selected.artifactIds.length}）</summary>
            {selected.artifactIds.length === 0 ? (
              <EmptyNote text="无产物(执行失败或未生成)。" />
            ) : (
              <div className="eval-thumbs">
                {selected.artifactIds.map((artifactId) => (
                  <a
                    key={artifactId}
                    href={api.artifactUrl(artifactId)}
                    target="_blank"
                    rel="noreferrer"
                  >
                    <img
                      className="eval-thumb"
                      src={api.artifactUrl(artifactId)}
                      alt={`产物 ${artifactId}`}
                      loading="lazy"
                    />
                  </a>
                ))}
              </div>
            )}
          </details>
          <h5>规则检查</h5>
          {selected.grades.length === 0 ? (
            <EmptyNote text="未评分" />
          ) : (
            <div className="eval-check-list">
              {[...selected.grades]
                .sort((a, b) => Number(a.status === 'pass') - Number(b.status === 'pass'))
                .map((grade) => (
                  <article className="eval-check-card" key={grade.gradeId}>
                    <header>
                      <strong title={grade.checkId}>
                        {checkLabel(grade.checkId)} <small>（{grade.checkId}）</small>
                      </strong>
                      <span
                        className={`eval-status-${grade.status === 'pass' ? 'pass' : grade.status === 'fail' ? 'fail' : 'pending'}`}
                      >
                        {grade.status === 'pass'
                          ? '通过'
                          : grade.status === 'fail'
                            ? '失败'
                            : '未判定'}
                      </span>
                    </header>
                    <dl>
                      <div>
                        <dt>期望</dt>
                        <dd>{resultLabel(grade.expected)}</dd>
                      </div>
                      <div>
                        <dt>实测</dt>
                        <dd>{resultLabel(grade.observed)}</dd>
                      </div>
                    </dl>
                    <details>
                      <summary>判分信息</summary>
                      <dl>
                        <div>
                          <dt>来源</dt>
                          <dd>{grade.source}</dd>
                        </div>
                        <div>
                          <dt>缓存</dt>
                          <dd>{grade.cacheHit ? '命中' : '本次计算'}</dd>
                        </div>
                      </dl>
                    </details>
                  </article>
                ))}
            </div>
          )}
          <details className="eval-technical-details">
            <summary>调用记录与步骤时间线</summary>
            <AttemptsPanel trialId={selected.trialId} />
            <h5>步骤时间线</h5>
            <StepsPanel runId={runId} trialId={selected.trialId} />
          </details>
        </div>
      )}
    </div>
  )
}

function AttemptsPanel({ trialId }: { trialId: string }) {
  const detailQuery = useEvalQuery(['eval-trial', trialId], () => api.trialDetail(trialId))
  if (detailQuery.isPending) {
    return <EmptyNote text="加载调用记录…" />
  }
  if (detailQuery.isError) {
    return <ErrorNote message={detailQuery.error.message} />
  }
  const attempts = detailQuery.data.attempts
  if (attempts.length === 0) {
    return <EmptyNote text="无外部调用记录(legacy 导入无过程数据)。" />
  }
  return (
    <div>
      <h5>调用记录(每次外部请求尝试)</h5>
      <EvaluationTable className="eval-table">
        <thead>
          <tr>
            <th>调用点</th>
            <th>#</th>
            <th>状态</th>
            <th>外部任务</th>
            <th>用量</th>
            <th>费用</th>
            <th>错误</th>
          </tr>
        </thead>
        <tbody>
          {attempts.map((attempt) => (
            <tr key={attempt.attemptId}>
              <td>{attempt.callSite}</td>
              <td>{attempt.attemptIndex}</td>
              <td>{attempt.status}</td>
              <td>{attempt.externalTaskId ?? '—'}</td>
              <td>
                {attempt.usage
                  ? `${attempt.usage.source}${attempt.usage.textTokens !== null ? ` / ${attempt.usage.textTokens} tok` : ''}`
                  : '—'}
              </td>
              <td>
                {attempt.cost && attempt.cost.amount !== null
                  ? `${attempt.cost.amount} ${attempt.cost.currency}(${attempt.cost.basis})`
                  : '—'}
              </td>
              <td>{attempt.error ?? '—'}</td>
            </tr>
          ))}
        </tbody>
      </EvaluationTable>
    </div>
  )
}

function RunsTab({
  initialRun,
  onCompare,
  onComparePair,
  onCreatePairRun,
}: {
  initialRun: string
  onCompare: (id: string) => void
  onComparePair: (pair: { baseline: string; candidate: string }) => void
  onCreatePairRun: (prefill: PairRunPrefill) => void
}) {
  const [current, setCurrent] = useState<string>(initialRun)
  const runsQuery = useEvalQuery(['eval-runs'], () => api.runs(), LIST_POLL_MS)
  const runs = [...(runsQuery.data?.runs ?? [])].sort((a, b) =>
    b.createdAt.localeCompare(a.createdAt),
  )
  useEffect(() => {
    if (!current && runs.length > 0) {
      setCurrent(runs[0].runId)
    }
  }, [runs, current])
  return (
    <section className="run-page">
      <h2>运行详情</h2>
      <label className="run-picker">
        选择运行
        <select value={current} onChange={(event) => setCurrent(event.target.value)}>
          {runs.length === 0 && <option value="">(暂无运行)</option>}
          {runs.map((run) => (
            <option key={run.runId} value={run.runId}>
              {run.runId}({run.mode} / {statusLabel(run.status)})
            </option>
          ))}
        </select>
      </label>
      {current ? (
        <RunDetailPanel
          key={current}
          runId={current}
          onCompare={onCompare}
          onComparePair={onComparePair}
          onCreatePairRun={onCreatePairRun}
        />
      ) : (
        <EmptyNote text="尚无可查看的运行。" />
      )}
    </section>
  )
}

function ReviewDetailPanel({ taskId }: { taskId: string }) {
  const reviewValue = (value: unknown): string => {
    const labels: Record<string, string> = {
      true: '是',
      false: '否',
      pass: '通过',
      fail: '失败',
      interrupted: '等待确认',
      completed: '已完成',
      cancelled: '已取消',
      inconclusive: '无法判定',
      error: '错误',
      dependency_failed: '前置检查失败',
      not_applicable: '不适用',
      interrupt_approval: '积分确认',
      stream_complete: '正常结束',
      stream_error: '执行错误',
      host_only: '直接生成',
      handoff_planner: '转交策划',
    }
    return value === null || value === undefined ? '—' : (labels[String(value)] ?? String(value))
  }
  const checkNames: Record<string, string> = {
    sse_terminal: '流结束事件',
    outcome: '执行结果',
    route: '任务路由',
    must_ask_user: '用户确认',
    tool_handoff_to_planner: '转交策划工具',
    success_artifacts: '成功作品数',
    draft_cleared: '续跑草稿已清除',
  }
  const [autoGrades, setAutoGrades] = useState<GradeView[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  // 转回归用例表单(§9.4):审核确认的问题 → 用例草稿 → 校验 → 冻结进回归集。
  const [draftPrompt, setDraftPrompt] = useState('')
  const [draftCheckId, setDraftCheckId] = useState('')
  const [draftQuestion, setDraftQuestion] = useState('')
  const [draftExpected, setDraftExpected] = useState('false')
  const [draftDataset, setDraftDataset] = useState('')
  const [draftBusy, setDraftBusy] = useState(false)
  const [draftMessage, setDraftMessage] = useState<string | null>(null)

  const detailQuery = useEvalQuery(['eval-review', taskId], () => api.reviewTaskDetail(taskId))
  const runId = detailQuery.data?.task.runId
  const definitions = useRunDefinitions(runId)
  const caseDefinition = definitions.data?.cases?.find(
    (item) => item.caseId === detailQuery.data?.task.caseId,
  )
  // 自动证据:同 run 的 trial 评分与任务绑定关系一致时展示,供比对人工意见。
  const autoTrialsQuery = useEvalQuery(
    ['eval-review-auto', runId ?? ''],
    () => api.trials(runId ?? ''),
    false,
    runId !== undefined,
  )
  useEffect(() => {
    if (detailQuery.data && autoTrialsQuery.data) {
      const trial = autoTrialsQuery.data.trials.find(
        (item) => item.trialId === detailQuery.data.task.trialId,
      )
      setAutoGrades(trial ? trial.grades : [])
    } else if (detailQuery.data && autoTrialsQuery.isError) {
      setAutoGrades([])
    }
  }, [detailQuery.data, autoTrialsQuery.data, autoTrialsQuery.isError])
  const detail = detailQuery.data ?? null

  const submitCaseDraft = useCallback(() => {
    if (!detail) {
      return
    }
    setDraftBusy(true)
    setError(null)
    setDraftMessage(null)
    const caseData = {
      schemaVersion: 2,
      caseId: detail.task.caseId,
      version: 1,
      language: 'zh-CN',
      taskType: 'text_to_image',
      title: `审核转用例:${detail.task.reviewTaskId}`,
      input: { prompt: draftPrompt, referenceArtifactIds: [], params: {} },
      checks: [
        {
          id: draftCheckId,
          kind: 'boolean',
          question: draftQuestion,
          expected: draftExpected === 'true',
        },
      ],
      provenance: { source: 'internal', note: `来自审核任务 ${detail.task.reviewTaskId}` },
    }
    api
      .createCaseDraft(taskId, caseData)
      .then((draft) => api.publishCaseDraft(draft.draftId, draftDataset, 'regression'))
      .then((manifest) =>
        setDraftMessage(
          `已冻结进回归集 ${manifest.datasetId}@v${manifest.version}(快照 ${manifest.contentHash.slice(0, 12)}…)`,
        ),
      )
      .catch((exc: unknown) => setError(exc instanceof Error ? exc.message : '转用例失败'))
      .finally(() => setDraftBusy(false))
  }, [detail, draftPrompt, draftCheckId, draftQuestion, draftExpected, draftDataset, taskId])

  const loadError = detailQuery.isError ? detailQuery.error.message : null
  if (loadError && !detail) {
    return <ErrorNote message={loadError} />
  }
  if (!detail) {
    return <EmptyNote text="加载任务详情…" />
  }
  const draftReady =
    draftPrompt.trim() !== '' &&
    draftCheckId.trim() !== '' &&
    draftQuestion.trim() !== '' &&
    draftDataset.trim() !== ''
  return (
    <div className="eval-review-detail">
      <h5>作品产物</h5>
      {detail.task.artifactIds.length === 0 ? (
        <EmptyNote
          text={
            detail.task.evidence?.suite
              ? '流程审核以 Trace 与规则证据为准，此任务未绑定作品。'
              : '此任务未绑定作品。'
          }
        />
      ) : (
        <div className="eval-thumbs">
          {detail.task.artifactIds.map((artifactId) => (
            <a key={artifactId} href={api.artifactUrl(artifactId)} target="_blank" rel="noreferrer">
              <img
                className="eval-thumb"
                src={api.artifactUrl(artifactId)}
                alt={`素材 ${artifactId}`}
                loading="lazy"
              />
            </a>
          ))}
        </div>
      )}
      <h5>规则与评分结果</h5>
      {autoGrades === null ? (
        <EmptyNote text="加载自动证据…" />
      ) : autoGrades.length === 0 ? (
        <EmptyNote text="无自动评分记录(执行失败或未评)。" />
      ) : (
        <EvaluationTable className="eval-table">
          <thead>
            <tr>
              <th>检查项</th>
              <th>期望</th>
              <th>实测</th>
              <th>判定</th>
            </tr>
          </thead>
          <tbody>
            {autoGrades.map((grade) => (
              <tr key={grade.gradeId}>
                <td>
                  <span>
                    {caseDefinition?.checks.find((check) => check.id === grade.checkId)?.question ??
                      checkNames[grade.checkId] ??
                      failureReason(grade.checkId)}
                  </span>
                  <small className="eval-data-code">{grade.checkId}</small>
                </td>
                <td>{reviewValue(grade.expected)}</td>
                <td>{reviewValue(grade.observed)}</td>
                <td>
                  <span className={`review-grade review-grade-${grade.status}`}>
                    {reviewValue(grade.status)}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </EvaluationTable>
      )}
      <h5>审核意见</h5>
      {detail.opinions.length === 0 ? (
        <EmptyNote text="尚无意见。" />
      ) : (
        <ul>
          {detail.opinions.map((opinion) => (
            <li key={opinion.opinionId}>
              <code>{opinion.reviewer}</code> rev{opinion.revision}:{' '}
              {opinion.verdicts
                .map((verdict) => `${verdict.checkId}=${verdict.verdict}`)
                .join(', ') || '(无逐项结论)'}
              {opinion.uncertain && ' [未定]'}
              {opinion.supersededBy && ' [已被取代]'}
              {opinion.note && ` — ${opinion.note}`}
            </li>
          ))}
        </ul>
      )}
      <h5>裁决记录</h5>
      {detail.adjudications.length === 0 ? (
        <EmptyNote text="尚未裁决。" />
      ) : (
        <ul>
          {detail.adjudications.map((adjudication) => (
            <li key={adjudication.adjudicationId}>
              <strong>{adjudication.finalVerdict}</strong> by {adjudication.decidedBy} —{' '}
              {adjudication.reason}(rubric v{adjudication.rubricVersion} {adjudication.at})
            </li>
          ))}
        </ul>
      )}
      {detail.disputes.length > 0 && (
        <>
          <h5>争议记录</h5>
          <ul>
            {detail.disputes.map((dispute, index) => (
              <li key={index}>
                {dispute.by}: {dispute.reason}
              </li>
            ))}
          </ul>
        </>
      )}
      <details className="review-regression">
        <summary>转为回归用例</summary>
        <div className="eval-draft-form">
          <label>
            提示词(复现问题的输入)
            <input value={draftPrompt} onChange={(e) => setDraftPrompt(e.target.value)} />
          </label>
          <label>
            检查项 ID(小写字母/数字/连字符)
            <input
              value={draftCheckId}
              onChange={(e) => setDraftCheckId(e.target.value)}
              placeholder="regression-check"
            />
          </label>
          <label>
            检查项问题
            <input value={draftQuestion} onChange={(e) => setDraftQuestion(e.target.value)} />
          </label>
          <label>
            期望
            <select value={draftExpected} onChange={(e) => setDraftExpected(e.target.value)}>
              <option value="false">false(问题复现)</option>
              <option value="true">true</option>
            </select>
          </label>
          <label>
            回归数据集 ID(冻结为新版本)
            <input
              value={draftDataset}
              onChange={(e) => setDraftDataset(e.target.value)}
              placeholder="regression-zh"
            />
          </label>
          <button type="button" disabled={draftBusy || !draftReady} onClick={submitCaseDraft}>
            校验并冻结进回归集
          </button>
          {draftMessage && <p className="eval-ok">{draftMessage}</p>}
        </div>
        <ErrorNote message={loadError ?? error} />
      </details>
    </div>
  )
}

function ReviewsTab({
  onRuns,
  onNew,
  initialRun = '',
}: {
  onRuns: () => void
  onNew: () => void
  initialRun?: string
}) {
  const [filter, setFilter] = useState('all')
  const [pendingAction, setPendingAction] = useState<{ taskId: string; action: string } | null>(
    null,
  )
  const [reviewRun, setReviewRun] = useState(initialRun)
  const reviewRuns = useEvalQuery(['eval-runs'], () => api.runs(), LIST_POLL_MS)
  const [opinion, setOpinion] = useState('同意自动判定')
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [reviewer, setReviewer] = useState(() => localStorage.getItem('eval-reviewer') ?? '')
  const [runId, setRunId] = useState('')
  const [trialId, setTrialId] = useState('')
  const [caseId, setCaseId] = useState('')
  const [expanded, setExpanded] = useState<string>('')
  const queryClient = useQueryClient()

  const tasksQuery = useEvalQuery(['eval-review-tasks'], () => api.reviewTasks(), LIST_POLL_MS)
  const tasks = (tasksQuery.data?.tasks ?? []).filter(
    (task) => !reviewRun || task.runId === reviewRun,
  )

  const refresh = useCallback(() => {
    return Promise.all([
      queryClient.invalidateQueries({ queryKey: ['eval-review-tasks'] }),
      queryClient.invalidateQueries({ queryKey: ['eval-review'] }),
    ])
  }, [queryClient])

  const createTask = useCallback(() => {
    setCreating(true)
    setError(null)
    api
      .createReviewTask({ runId, trialId, caseId, priority: 'normal' })
      .then(() => refresh())
      .catch((exc: unknown) => setError(exc instanceof ApiError ? exc.message : String(exc)))
      .finally(() => setCreating(false))
  }, [runId, trialId, caseId, refresh])

  const act = useCallback(
    (task: ReviewTaskView, action: 'claim' | 'submit' | 'dispute' | 'adjudicate') => {
      if (pendingAction) {
        return
      }
      const finish = (promise: Promise<unknown>) =>
        promise
          .then(() => refresh())
          .catch((exc: unknown) => setError(exc instanceof ApiError ? exc.message : String(exc)))
          .finally(() => setPendingAction(null))
      if (!reviewer.trim()) {
        setError('请先填写审核人身份(领取与提交都需要身份关联)')
        return
      }
      setError(null)
      setPendingAction({ taskId: task.reviewTaskId, action })
      if (action === 'claim') {
        void finish(api.claimReview(task.reviewTaskId, reviewer, task.revision))
      } else if (action === 'submit') {
        void finish(
          api.submitReview(task.reviewTaskId, {
            reviewer,
            expectedRevision: task.revision,
            verdicts: [],
            agreeWithAuto: opinion === '同意自动判定',
            uncertain: opinion !== '同意自动判定',
            note:
              opinion === '同意自动判定' && task.evidence?.reasons.length
                ? '同意自动失败'
                : opinion,
          }),
        )
      } else if (action === 'dispute') {
        void finish(api.disputeReview(task.reviewTaskId, reviewer, '页面发起争议'))
      } else {
        void finish(
          api.adjudicate(task.reviewTaskId, {
            boundOpinionIds: [],
            finalVerdict: 'undetermined',
            reason: '页面裁决(待人工补充理由)',
            decidedBy: reviewer,
          }),
        )
      }
    },
    [reviewer, refresh, opinion, pendingAction],
  )

  const visibleTasks = tasks.filter(
    (task) =>
      filter === 'all' ||
      (filter === 'pending'
        ? task.status === 'pending' || task.status === 'claimed'
        : task.status !== 'pending' && task.status !== 'claimed'),
  )
  const selected = visibleTasks.find((task) => task.reviewTaskId === expanded) ?? visibleTasks[0]
  const ownedByMe = Boolean(selected?.claim && selected.claim.reviewer === reviewer)
  const expiredClaim = Boolean(
    selected?.claim && Date.parse(selected.claim.leaseUntil) <= Date.now(),
  )
  const canClaim =
    selected?.status === 'pending' ||
    (selected?.status === 'claimed' && (ownedByMe || expiredClaim || !selected.claim))
  const claiming =
    pendingAction?.taskId === selected?.reviewTaskId && pendingAction?.action === 'claim'
  const claimLabel =
    selected?.status === 'pending' ? '领取任务' : expiredClaim ? '重新领取' : '延长领取时间'
  const priorities: Record<string, string> = {
    required: '必审',
    risk: '风险',
    random: '随机',
    normal: '手动',
  }
  const states: Record<string, string> = {
    pending: '待领取',
    claimed: '审核中',
    submitted: '已提交',
    disputed: '有争议',
    adjudicated: '已裁决',
  }
  return (
    <section className="review-workspace">
      <div className="review-heading">
        <div>
          <h2>人工审核</h2>
          <span>{tasks.length} 项任务</span>
        </div>
        <label>
          审核人
          <input
            value={reviewer}
            placeholder="alice"
            onChange={(event) => {
              setReviewer(event.target.value)
              localStorage.setItem('eval-reviewer', event.target.value)
            }}
          />
        </label>
      </div>
      <ErrorNote message={error ?? tasksQuery.error?.message ?? null} />
      <label>
        审核运行
        <select value={reviewRun} onChange={(event) => setReviewRun(event.target.value)}>
          <option value="">全部运行</option>
          {(reviewRuns.data?.runs ?? []).map((run) => (
            <option key={run.runId} value={run.runId}>
              {run.runId} · {statusLabel(run.status)}
            </option>
          ))}
        </select>
      </label>
      <div className="review-topbar">
        <div className="review-filters">
          {[
            ['all', '全部'],
            ['pending', '待处理'],
            ['done', '已处理'],
          ].map(([id, label]) => (
            <button
              type="button"
              key={id}
              className={filter === id ? 'active' : ''}
              onClick={() => setFilter(id)}
            >
              {label}
            </button>
          ))}
        </div>
        <details className="review-create">
          <summary>＋ 创建审核任务</summary>
          <div className="review-create-form">
            <label>
              运行 ID
              <input value={runId} onChange={(e) => setRunId(e.target.value)} />
            </label>
            <label>
              Trial ID
              <input value={trialId} onChange={(e) => setTrialId(e.target.value)} />
            </label>
            <label>
              用例 ID
              <input value={caseId} onChange={(e) => setCaseId(e.target.value)} />
            </label>
            <button
              type="button"
              disabled={creating || !runId.trim() || !trialId.trim() || !caseId.trim()}
              onClick={createTask}
            >
              {creating ? '创建中…' : '创建审核任务'}
            </button>
          </div>
        </details>
      </div>
      {tasks.length === 0 ? (
        <div className="eval-review-empty">
          <h3>{reviewRun ? '本 run 未命中送审策略' : '未命中自动送审策略'}</h3>
          <p>已完成的 run 未命中送审策略时不会创建任务；进行中的 run 会在结束后自动判断。</p>
          <p>
            效果题的裁判异常、模糊带或标记题会自动送审；Agent 规则 PASS/FAIL 和 mock/stub
            自测默认不送。
          </p>
          <button onClick={onRuns}>查看运行与送审结果</button>
          <button onClick={onNew}>去新建/查看评测</button>
        </div>
      ) : (
        <div className="review-split">
          <div className="review-queue">
            {visibleTasks.length === 0 && <EmptyNote text="没有符合筛选的任务" />}
            {visibleTasks.map((task) => (
              <button
                type="button"
                key={task.reviewTaskId}
                className={`review-task ${selected?.reviewTaskId === task.reviewTaskId ? 'selected' : ''}`}
                onClick={() => setExpanded(task.reviewTaskId)}
              >
                <div className="review-task-tags">
                  <span>
                    {priorities[task.priority] ?? task.priority}
                    {task.evidence?.suite ? ` · ${task.evidence.suite}` : ''}
                  </span>
                  <span className="review-task-state" data-status={task.status}>
                    {states[task.status] ?? task.status}
                  </span>
                </div>
                <strong>
                  <RunCaseIdentity runId={task.runId} caseId={task.caseId} />
                </strong>
                <span className="review-run-id">{task.runId}</span>
                <span className="review-reason">
                  {reviewReason(task.sampling.rule)} <small>（{task.sampling.rule}）</small>
                </span>
                {task.evidence?.reasons.length ? (
                  <span className="review-reason">
                    <RunCheckLabel
                      runId={task.runId}
                      caseId={task.caseId}
                      checkId={task.evidence.reasons[0]}
                    />
                  </span>
                ) : null}
              </button>
            ))}
          </div>
          {selected && (
            <article className="review-inspector" key={selected.reviewTaskId}>
              <div className="review-inspector-heading">
                <div>
                  <span className="review-eyebrow">
                    {selected.evidence?.suite || '结果复核'} · {priorities[selected.priority]}
                  </span>
                  <h3>
                    <RunCaseIdentity runId={selected.runId} caseId={selected.caseId} />
                  </h3>
                  <p>
                    {reviewReason(selected.sampling.rule)}{' '}
                    <small>（{selected.sampling.rule}）</small>
                  </p>
                </div>
                <div className="review-primary-action">
                  <span className="review-status" data-status={selected.status} role="status">
                    <span className="review-status-dot" aria-hidden="true" />
                    {claiming ? '正在领取' : (states[selected.status] ?? selected.status)}
                  </span>
                  {canClaim && (
                    <button
                      type="button"
                      className={`review-claim-button ${selected.status === 'pending' ? 'eval-primary' : ''}`}
                      disabled={pendingAction !== null}
                      aria-busy={claiming}
                      onClick={() => act(selected, 'claim')}
                    >
                      {claiming ? '领取中…' : claimLabel}
                    </button>
                  )}
                  {selected.status === 'claimed' && selected.claim && (
                    <span className="review-claim-owner">
                      {ownedByMe ? '由你审核' : `由 ${selected.claim.reviewer} 审核`}
                    </span>
                  )}
                </div>
              </div>
              <div className="review-context">
                <span>本次运行抽样</span>
                <b>
                  必审{' '}
                  {
                    tasks.filter((t) => t.runId === selected.runId && t.priority === 'required')
                      .length
                  }
                </b>
                <b>
                  风险{' '}
                  {tasks.filter((t) => t.runId === selected.runId && t.priority === 'risk').length}
                </b>
                <b>
                  随机{' '}
                  {
                    tasks.filter((t) => t.runId === selected.runId && t.priority === 'random')
                      .length
                  }
                </b>
              </div>
              {selected.evidence?.reasons.length ? (
                <div className="review-failure">
                  <strong>自动判定依据</strong>
                  {selected.evidence.reasons.map((reason) => (
                    <RunCheckLabel
                      key={reason}
                      runId={selected.runId}
                      caseId={selected.caseId}
                      checkId={reason}
                    />
                  ))}
                </div>
              ) : null}
              <div className="review-evidence-toolbar">
                {selected.evidence?.traceUrl && (
                  <a href={selected.evidence.traceUrl} target="_blank" rel="noreferrer">
                    查看完整 Trace ↗
                  </a>
                )}
                <details>
                  <summary>任务标识</summary>
                  <dl>
                    <dt>运行</dt>
                    <dd>{selected.runId}</dd>
                    <dt>Trial</dt>
                    <dd>{selected.trialId}</dd>
                    <dt>任务</dt>
                    <dd>{selected.reviewTaskId}</dd>
                    {selected.evidence?.tracePath && (
                      <>
                        <dt>Trace</dt>
                        <dd>{selected.evidence.tracePath}</dd>
                      </>
                    )}
                  </dl>
                </details>
              </div>
              <ReviewDetailPanel taskId={selected.reviewTaskId} />
              <div className="review-decision">
                <label>
                  审核结论
                  <select value={opinion} onChange={(e) => setOpinion(e.target.value)}>
                    {['同意自动判定', '标记用例有问题', '标记判分器有问题'].map((value) => (
                      <option key={value}>{value}</option>
                    ))}
                  </select>
                </label>
                <div>
                  <button
                    type="button"
                    className="eval-primary"
                    disabled={pendingAction !== null}
                    onClick={() => act(selected, 'submit')}
                  >
                    提交意见
                  </button>
                  <details>
                    <summary>更多操作</summary>
                    <button type="button" onClick={() => act(selected, 'dispute')}>
                      发起争议
                    </button>
                    <button type="button" onClick={() => act(selected, 'adjudicate')}>
                      裁决
                    </button>
                  </details>
                </div>
              </div>
            </article>
          )}
        </div>
      )}
    </section>
  )
}

function OpsTab() {
  const [flushMessage, setFlushMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const queryClient = useQueryClient()

  const healthQuery = useEvalQuery(['eval-health'], () => api.health(), OPS_POLL_MS)
  const alertsQuery = useEvalQuery(['eval-alerts'], () => api.alerts(), OPS_POLL_MS)
  const outboxQuery = useEvalQuery(['eval-outbox'], () => api.outboxStatus(), OPS_POLL_MS)
  const calibrationsQuery = useEvalQuery(['eval-calibrations'], () => api.calibrations())

  const reload = useCallback(() => {
    void queryClient.invalidateQueries({
      queryKey: ['eval-health'],
    })
    void queryClient.invalidateQueries({ queryKey: ['eval-alerts'] })
    void queryClient.invalidateQueries({ queryKey: ['eval-outbox'] })
    void queryClient.invalidateQueries({ queryKey: ['eval-calibrations'] })
  }, [queryClient])

  const flush = useCallback(() => {
    api
      .outboxFlush()
      .then((result) => {
        setFlushMessage(
          result.configured
            ? `已导出 ${result.flushed} 条,剩余 ${result.remaining}`
            : `未配置 Langfuse,未导出;积压 ${result.remaining} 条。${result.lastError ?? ''}`,
        )
        reload()
      })
      .catch((exc: unknown) => setError(exc instanceof Error ? exc.message : '导出失败'))
  }, [reload])

  if (healthQuery.isError && !healthQuery.data) {
    return <ErrorNote message={healthQuery.error.message} />
  }
  const health = healthQuery.data ?? null
  const alerts = alertsQuery.data ?? null
  const outbox = outboxQuery.data ?? null
  const calibrations = calibrationsQuery.data?.calibrations ?? null
  const loadError = error
  return (
    <section className="eval-ops-page">
      <div className="eval-page-heading">
        <h2>运营状态</h2>
        <button type="button" onClick={reload}>
          刷新状态
        </button>
      </div>
      <ErrorNote message={loadError} />
      <div className="eval-health-card">
        <h3>系统健康</h3>
        {health === null ? (
          <EmptyNote text="加载健康指标…" />
        ) : (
          <EvaluationTable className="eval-table">
            <tbody>
              <tr>
                <th>活跃 run</th>
                <td>{health.activeRuns}</td>
                <th>卡住 trial(&gt;1h)</th>
                <td>{health.stuckTrials.length}</td>
              </tr>
              <tr>
                <th>上游未知结果 trial</th>
                <td>{health.unknownExternalTrials}</td>
                <th>未结预算预留 / 未知结果</th>
                <td>
                  {health.budget.unsettledReservations} / {health.budget.unknownOutcomes}
                </td>
              </tr>
              <tr>
                <th>裁判错误率</th>
                <td>
                  {health.judgedTrials === 0
                    ? 'N/A'
                    : `${((health.judgeErrorTrials / health.judgedTrials) * 100).toFixed(1)}%`}
                </td>
                <th>最久待审</th>
                <td>
                  {health.oldestPendingReviewHours === null
                    ? 'N/A'
                    : `${health.oldestPendingReviewHours}h`}
                </td>
              </tr>
            </tbody>
          </EvaluationTable>
        )}
      </div>
      {alerts && alerts.alerts.length > 0 && (
        <div className="eval-gaps">
          <h4>告警</h4>
          <ul>
            {alerts.alerts.map((alert) => (
              <li key={alert.code}>
                <strong>{alert.code}</strong>: {alert.message}
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className="eval-ops-grid">
        <div className="eval-outbox-card">
          <h3>观测导出</h3>
          {outbox && (
            <div className="eval-export-status">
              <div>
                <span>待导出记录</span>
                <strong>{outbox.pending} 条</strong>
              </div>
              <div>
                <span>Langfuse</span>
                <strong>{outbox.langfuseConfigured ? '已配置' : '未配置'}</strong>
              </div>
              <button type="button" onClick={flush}>
                手动导出重试
              </button>
            </div>
          )}
          {flushMessage && <p className="eval-muted">{flushMessage}</p>}
        </div>
        <div className="eval-calibration-card">
          <h3>裁判校准</h3>
          {calibrations === null ? (
            <EmptyNote text="加载中…" />
          ) : calibrations.length === 0 ? (
            <EmptyNote text="暂无校准记录" />
          ) : (
            <EvaluationTable className="eval-table">
              <thead>
                <tr>
                  <th>校准 ID</th>
                  <th>裁判模型</th>
                  <th>样本数</th>
                  <th>缺陷召回</th>
                  <th>误报率</th>
                  <th>排除(未定)</th>
                </tr>
              </thead>
              <tbody>
                {calibrations.map((calibration) => (
                  <tr key={calibration.calibrationId}>
                    <td>
                      <code>{calibration.calibrationId}</code>
                    </td>
                    <td>{calibration.judgeModel}</td>
                    <td>{calibration.sampleCount}</td>
                    <td>
                      {calibration.report.defectRecall === null
                        ? 'N/A'
                        : percent(calibration.report.defectRecall)}
                    </td>
                    <td>
                      {calibration.report.falsePositiveRate === null
                        ? 'N/A'
                        : percent(calibration.report.falsePositiveRate)}
                    </td>
                    <td>
                      {calibration.report.excluded.autoUnknown +
                        calibration.report.excluded.humanUnknown}
                    </td>
                  </tr>
                ))}
              </tbody>
            </EvaluationTable>
          )}
        </div>
      </div>
    </section>
  )
}

export function EvaluationApp() {
  // 查询客户端随应用自持:证据读取失败立刻显式呈现,不自动重试、不随窗口聚焦刷新。
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
      }),
  )
  const [tab, setTab] = useState<Tab>(() => {
    const requested =
      location.pathname === '/internal/runs'
        ? 'diagnostics'
        : new URLSearchParams(location.search).get('tab')
    return TABS.some((item) => item.id === requested) ? (requested as Tab) : 'overview'
  })
  const [reviewScope, setReviewScope] = useState('')
  const openReviews = (id: string) => {
    setReviewScope(id)
    setTab('reviews')
  }
  const [comparisonRun, setComparisonRun] = useState('')
  const [comparisonPair, setComparisonPair] = useState<{
    baseline: string
    candidate: string
  } | null>(null)
  const openComparison = useCallback((id: string) => {
    setComparisonRun(id)
    setComparisonPair(null)
    setTab('cost')
  }, [])
  const openPairComparison = useCallback((pair: { baseline: string; candidate: string }) => {
    setComparisonPair(pair)
    setComparisonRun('')
    setTab('cost')
  }, [])
  const [newRunPrefill, setNewRunPrefill] = useState<PairRunPrefill | null>(null)
  const openCreatePairRun = useCallback((prefill: PairRunPrefill) => {
    setNewRunPrefill(prefill)
    setTab('new')
  }, [])
  const [pendingRun, setPendingRun] = useState<string>('')
  const openRun = useCallback((runId: string) => {
    setPendingRun(runId)
    setTab('runs')
  }, [])
  const runsTab = useMemo(
    () => (
      <RunsTab
        key={pendingRun}
        initialRun={pendingRun}
        onCompare={openComparison}
        onComparePair={openPairComparison}
        onCreatePairRun={openCreatePairRun}
      />
    ),
    [pendingRun, openComparison, openPairComparison, openCreatePairRun],
  )
  return (
    <QueryClientProvider client={queryClient}>
      <div className="eval-app">
        <header>
          <a className="eval-brand" href="/">
            FRAYUNE <span>帧屿集</span>
          </a>
          <h2>评测工作台</h2>
          <nav>
            {TABS.filter(
              (item) => location.pathname !== '/internal/runs' || item.id === 'diagnostics',
            ).map((item) => (
              <button
                key={item.id}
                type="button"
                className={tab === item.id ? 'eval-tab-active' : ''}
                onClick={() => setTab(item.id)}
              >
                {item.label}
              </button>
            ))}
          </nav>
          <a className="eval-back" href="https://127.0.0.1:8443/">
            ← 返回创作工作台
          </a>
        </header>
        <main>
          {tab === 'overview' && (
            <FlowOverview
              onOpenRun={openRun}
              onQuality={() => setTab('new')}
              onReviews={openReviews}
            />
          )}
          {tab === 'datasets' && <OverviewTab onOpenRun={openRun} />}
          {tab === 'new' && (
            <NewRunTab
              key={
                newRunPrefill
                  ? `prefill:${newRunPrefill.datasetId}:${newRunPrefill.compression.enabled}:${newRunPrefill.purpose}`
                  : 'plain'
              }
              onCreated={openRun}
              onReviews={openReviews}
              prefill={newRunPrefill}
            />
          )}
          {tab === 'runs' && runsTab}
          {tab === 'reviews' && (
            <ReviewsTab
              initialRun={reviewScope}
              onRuns={() => setTab('runs')}
              onNew={() => setTab('new')}
            />
          )}
          {tab === 'compare' && <ComparisonWorkspace mode="compare" onOpenRun={openRun} />}
          {tab === 'cost' && (
            <ComparisonWorkspace
              key={
                comparisonPair
                  ? `pair:${comparisonPair.baseline}:${comparisonPair.candidate}`
                  : `run:${comparisonRun}`
              }
              initialRun={comparisonRun}
              initialPair={comparisonPair}
              auto
              onOpenRun={openRun}
              onCreatePairRun={openCreatePairRun}
            />
          )}
          {tab === 'ops' && <OpsTab />}
          {tab === 'diagnostics' && <RunDiagnostics />}
        </main>
      </div>
    </QueryClientProvider>
  )
}
