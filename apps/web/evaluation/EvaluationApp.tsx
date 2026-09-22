/** FRAYUNE 评测工作台(§15):数据集/新建/运行详情/审核/比较/成本与压缩/运营。

交互纪律:
- 创建 run 立即返回(后台调度),列表与详情轮询刷新;刷新页面后可继续追踪。
- 长任务不阻塞页面:所有按钮异步反馈,错误/空态/加载态显式呈现。
- 未配置的能力(live 密钥、Langfuse)如实显示"未配置",不显示已接通。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { QueryClient, QueryClientProvider, useQueryClient } from '@tanstack/react-query'
import { ACTIVE_POLL_MS, LIST_POLL_MS, OPS_POLL_MS, useEvalQuery } from './queries.js'
import {
  api,
  ApiError,
  type ComparisonView,
  type DatasetInfo,
  type GradeView,
  type CompressionReport,
  type ReviewTaskView,
  type RunDetail,
  type TrialView,
} from './api.js'

type Tab = 'overview' | 'new' | 'runs' | 'reviews' | 'compare' | 'cost' | 'ops'

const TABS: { id: Tab; label: string }[] = [
  { id: 'overview', label: '评测总览' },
  { id: 'new', label: '新建评测' },
  { id: 'runs', label: '运行详情' },
  { id: 'reviews', label: '人工审核' },
  { id: 'compare', label: '批次比较' },
  { id: 'cost', label: '成本与压缩' },
  { id: 'ops', label: '运营状态' },
]

const ACTIVE_STATUSES = new Set(['queued', 'running', 'stopping'])

export const STATUS_LABELS: Record<string, string> = {
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
      <h2>评测总览</h2>
      <h3>数据集(冻结版本)</h3>
      {datasetsQuery.isPending ? (
        <EmptyNote text="加载中…" />
      ) : datasets === null || datasets.length === 0 ? (
        <EmptyNote text="尚无冻结数据集;先用 CLI 冻结:python scripts/eval.py dataset freeze <文件> <数据集ID>。" />
      ) : (
        <table className="eval-table">
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
                  <td>{dataset.datasetId}</td>
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
        </table>
      )}
      <h3>运行记录(每 4 秒刷新)</h3>
      {runsQuery.isPending ? (
        <EmptyNote text="加载中…" />
      ) : runs === null || runs.length === 0 ? (
        <EmptyNote text="尚无评测运行。" />
      ) : (
        <table className="eval-table">
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
        </table>
      )}
    </section>
  )
}

function NewRunTab({ onCreated }: { onCreated: (runId: string) => void }) {
  const [datasets, setDatasets] = useState<DatasetInfo[]>([])
  const [datasetId, setDatasetId] = useState('')
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
  const [compression, setCompression] = useState(false)
  const [useCache, setUseCache] = useState(true)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
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

  const submit = useCallback(() => {
    setBusy(true)
    setError(null)
    setMessage(null)
    const body: Parameters<typeof api.createRun>[0] = liveMode
      ? {
          runId,
          datasetId,
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
      : { runId, datasetId, repetitions, purpose, mode: 'mock', useGradeCache: useCache }
    api
      .createRun(body)
      .then((result) => {
        setMessage(
          `已创建 ${result.runId}(状态 ${statusLabel(result.status)});后台执行中,可在"运行详情"轮询。`,
        )
        onCreated(result.runId)
      })
      .catch((exc: unknown) => setError(exc instanceof ApiError ? exc.message : String(exc)))
      .finally(() => setBusy(false))
  }, [
    liveMode,
    runId,
    datasetId,
    repetitions,
    purpose,
    maxCost,
    genPrice,
    judgePrice,
    judgeModel,
    compression,
    useCache,
    onCreated,
  ])

  const submitReplay = useCallback(() => {
    setBusy(true)
    setError(null)
    setMessage(null)
    api
      .createRun({
        runId,
        mode: 'replay',
        sourceRunId: replaySource,
        recordingId: recordingId || undefined,
        purpose: purpose || '严格回放',
      })
      .then((result) => {
        setMessage(`严格回放已创建 ${result.runId}(禁网;差异见运行详情的回放证据)。`)
        onCreated(result.runId)
      })
      .catch((exc: unknown) => setError(exc instanceof ApiError ? exc.message : String(exc)))
      .finally(() => setBusy(false))
  }, [runId, replaySource, recordingId, purpose, onCreated])

  return (
    <section>
      <h2>新建评测</h2>
      <p className="eval-note">
        Mock 与回放零费用。live 默认禁止:需显式预算与密钥(SWARMUI_IMAGE_API_KEY /
        EVAL_JUDGE_API_KEY), 未配置时执行会以明确错误停止,不会静默转免费。默认预算 0(§13.2)。
      </p>
      <ErrorNote message={error} />
      {message && <div className="eval-ok">{message}</div>}
      <h3>生成运行(Mock / Live)</h3>
      <label className="eval-checkbox">
        <input type="checkbox" checked={liveMode} onChange={(e) => setLiveMode(e.target.checked)} />
        live 模式(真实调用,产生费用)
      </label>
      {liveMode && (
        <div className="eval-live-config">
          <label>
            预算上限(CNY,必填正数)
            <input value={maxCost} onChange={(e) => setMaxCost(e.target.value)} placeholder="5.0" />
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
            vlm 裁判模型(可选;留空使用 stub 判分)
            <input
              value={judgeModel}
              onChange={(e) => setJudgeModel(e.target.value)}
              placeholder="qwen-vl-max-latest"
            />
          </label>
          <p className="eval-note">
            未登记价格的调用会被硬预算拒绝发送(PRICE_UNKNOWN),不会盲目计费。
          </p>
        </div>
      )}
      <label className="eval-checkbox">
        <input type="checkbox" checked={useCache} onChange={(e) => setUseCache(e.target.checked)} />
        启用判分缓存(稳定性试验请关闭)
      </label>
      <label className="eval-checkbox">
        <input
          type="checkbox"
          checked={compression}
          onChange={(e) => setCompression(e.target.checked)}
        />
        启用提示词压缩实验(whitespace-v1;可能改变语义,结果需配对比较)
      </label>
      <label>
        数据集
        <select value={datasetId} onChange={(event) => setDatasetId(event.target.value)}>
          {datasets.map((dataset) => (
            <option key={dataset.datasetId} value={dataset.datasetId}>
              {dataset.datasetId}(最新 v
              {dataset.versions[dataset.versions.length - 1]?.version ?? '?'})
            </option>
          ))}
        </select>
      </label>
      <label>
        runId(每次运行需新 ID)
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
      <button type="button" disabled={busy || !runId || (!liveMode && !datasetId)} onClick={submit}>
        {liveMode ? '预检并启动 live 运行' : '预检并启动 Mock 运行'}
      </button>

      <h3>严格回放(禁网)</h3>
      <label>
        源 runId
        <input value={replaySource} onChange={(event) => setReplaySource(event.target.value)} />
      </label>
      <label>
        录制 ID(留空使用源 run 默认录制)
        <input value={recordingId} onChange={(event) => setRecordingId(event.target.value)} />
      </label>
      <button type="button" disabled={busy || !runId || !replaySource} onClick={submitReplay}>
        启动严格回放
      </button>
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
      <table className="eval-table">
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
      </table>
      {budget.project && (
        <p className="eval-muted">
          项目总预算:已结算 {money(budget.project.settled)},未结预留{' '}
          {money(budget.project.outstandingReserved)},未知 {budget.project.unknownCount}
        </p>
      )}
      {budget.reservations && budget.reservations.length > 0 && (
        <table className="eval-table">
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
        </table>
      )}
    </div>
  )
}

function RunDetailPanel({ runId }: { runId: string }) {
  const [selected, setSelected] = useState<TrialView | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
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
  return (
    <div>
      <h3>
        run <code>{runId}</code>({detail.manifest.mode} / {detail.manifest.provider} / 状态{' '}
        {statusLabel(detail.status)})
      </h3>
      <p className="eval-muted">
        进度:完成 {detail.progress.completed}/{detail.progress.total};运行中{' '}
        {detail.progress.running}; 排队 {detail.progress.pending};失败 {detail.progress.failed};取消{' '}
        {detail.progress.cancelled}
        {detail.manifest.parentRunId && (
          <>
            ;回放自 <code>{detail.manifest.parentRunId}</code>
          </>
        )}
      </p>
      {detail.extra.error && <ErrorNote message={detail.extra.error} />}
      <ul className="eval-annotations">
        {detail.annotations.map((note) => (
          <li key={note}>⚠ {note}</li>
        ))}
      </ul>
      <p>
        数据集{' '}
        <code>
          {detail.manifest.dataset.datasetId}@v{detail.manifest.dataset.version}
        </code>
        (快照 {detail.manifest.dataset.contentHash.slice(0, 12)}…);计划 {detail.denominators.plan}{' '}
        条
        {detail.manifest.compression.enabled && (
          <strong>;压缩实验已启用({detail.manifest.compression.compressorVersion})</strong>
        )}
      </p>
      <table className="eval-table">
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
      </table>
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
      <h4>逐 trial 明细</h4>
      <table className="eval-table">
        <thead>
          <tr>
            <th>用例</th>
            <th>重复</th>
            <th>执行</th>
            <th>质量</th>
            <th>加权分</th>
            <th>seed(请求/实际)</th>
          </tr>
        </thead>
        <tbody>
          {trials.map((trial) => (
            <tr
              key={trial.trialId}
              className={selected?.trialId === trial.trialId ? 'eval-selected' : ''}
              onClick={() => setSelected(trial)}
            >
              <td>{trial.caseId}</td>
              <td>{trial.repetitionIndex}</td>
              <td>{trial.status}</td>
              <td>{trial.qualityVerdict ?? '未评分'}</td>
              <td>{trial.weightedScore === null ? 'N/A(未评)' : trial.weightedScore.toFixed(2)}</td>
              <td>
                {trial.requestedSeed}/{trial.resolvedSeed ?? '?'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {selected && (
        <div className="eval-trial-detail">
          <h4>
            用例详情 <code>{selected.caseId}</code>
          </h4>
          {selected.error && <div className="eval-error">执行错误:{selected.error}</div>}
          <h5>产物</h5>
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
          <h5>逐项评分(自动/类型化规则)</h5>
          {selected.grades.length === 0 ? (
            <EmptyNote text="未评分:执行失败或未评,不显示 0 分(§15.2)。" />
          ) : (
            <table className="eval-table">
              <thead>
                <tr>
                  <th>检查项</th>
                  <th>期望</th>
                  <th>实测</th>
                  <th>判定</th>
                  <th>来源</th>
                  <th>缓存</th>
                </tr>
              </thead>
              <tbody>
                {selected.grades.map((grade) => (
                  <tr key={grade.gradeId}>
                    <td>{grade.checkId}</td>
                    <td>{String(grade.expected)}</td>
                    <td>{String(grade.observed)}</td>
                    <td>{grade.status}</td>
                    <td>{grade.source}</td>
                    <td>{grade.cacheHit ? '命中' : '本次'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <AttemptsPanel trialId={selected.trialId} />
          <h5>步骤时间线</h5>
          <StepsPanel runId={runId} trialId={selected.trialId} />
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
      <table className="eval-table">
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
      </table>
    </div>
  )
}

function RunsTab({ initialRun }: { initialRun: string }) {
  const [current, setCurrent] = useState<string>(initialRun)
  const runsQuery = useEvalQuery(['eval-runs'], () => api.runs(), LIST_POLL_MS)
  const runs = runsQuery.data?.runs ?? []
  useEffect(() => {
    if (!current && runs.length > 0) {
      setCurrent(runs[0].runId)
    }
  }, [runs, current])
  return (
    <section>
      <h2>运行详情</h2>
      <select value={current} onChange={(event) => setCurrent(event.target.value)}>
        {runs.length === 0 && <option value="">(暂无运行)</option>}
        {runs.map((run) => (
          <option key={run.runId} value={run.runId}>
            {run.runId}({run.mode} / {statusLabel(run.status)})
          </option>
        ))}
      </select>
      {current ? <RunDetailPanel runId={current} /> : <EmptyNote text="尚无可查看的运行。" />}
    </section>
  )
}

function ReviewDetailPanel({ taskId }: { taskId: string }) {
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
      <h5>素材(自动评分与人工意见的判定对象)</h5>
      {detail.task.artifactIds.length === 0 ? (
        <EmptyNote text="任务未绑定素材。" />
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
      <h5>自动证据(独立意见与之比对;不覆盖自动评分)</h5>
      {autoGrades === null ? (
        <EmptyNote text="加载自动证据…" />
      ) : autoGrades.length === 0 ? (
        <EmptyNote text="无自动评分记录(执行失败或未评)。" />
      ) : (
        <table className="eval-table">
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
                <td>{grade.checkId}</td>
                <td>{String(grade.expected)}</td>
                <td>{String(grade.observed)}</td>
                <td>{grade.status}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <h5>独立意见(追加式,修订保留历史)</h5>
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
      <h5>裁决(绑定意见与规则版本)</h5>
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
      <h5>确认问题 → 固化为回归用例(§9.4)</h5>
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
    </div>
  )
}

function ReviewsTab() {
  const [error, setError] = useState<string | null>(null)
  const [reviewer, setReviewer] = useState('')
  const [runId, setRunId] = useState('')
  const [trialId, setTrialId] = useState('')
  const [caseId, setCaseId] = useState('')
  const [expanded, setExpanded] = useState<string>('')
  const queryClient = useQueryClient()

  const tasksQuery = useEvalQuery(['eval-review-tasks'], () => api.reviewTasks())
  const tasks = tasksQuery.data?.tasks ?? []

  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['eval-review-tasks'] })
    void queryClient.invalidateQueries({ queryKey: ['eval-review'] })
  }, [queryClient])

  const createTask = useCallback(() => {
    api
      .createReviewTask({ runId, trialId, caseId, priority: 'required' })
      .then(() => refresh())
      .catch((exc: unknown) => setError(exc instanceof ApiError ? exc.message : String(exc)))
  }, [runId, trialId, caseId, refresh])

  const act = useCallback(
    (task: ReviewTaskView, action: 'claim' | 'submit' | 'dispute' | 'adjudicate') => {
      const finish = (promise: Promise<unknown>) =>
        promise
          .then(() => refresh())
          .catch((exc: unknown) => setError(exc instanceof ApiError ? exc.message : String(exc)))
      if (!reviewer) {
        setError('请先填写审核人身份(领取与提交都需要身份关联)')
        return
      }
      if (action === 'claim') {
        void finish(api.claimReview(task.reviewTaskId, reviewer, task.revision))
      } else if (action === 'submit') {
        void finish(
          api.submitReview(task.reviewTaskId, {
            reviewer,
            expectedRevision: task.revision,
            verdicts: [],
            agreeWithAuto: null,
            uncertain: true,
            note: '页面提交:请补充逐项结论',
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
    [reviewer, refresh],
  )

  return (
    <section>
      <h2>人工审核</h2>
      <ErrorNote message={error} />
      <h3>从运行结果创建审核任务</h3>
      <label>
        runId
        <input value={runId} onChange={(event) => setRunId(event.target.value)} />
      </label>
      <label>
        trialId
        <input value={trialId} onChange={(event) => setTrialId(event.target.value)} />
      </label>
      <label>
        caseId
        <input value={caseId} onChange={(event) => setCaseId(event.target.value)} />
      </label>
      <label>
        审核人(用于领取租约与意见身份关联)
        <input
          value={reviewer}
          onChange={(event) => setReviewer(event.target.value)}
          placeholder="alice"
        />
      </label>
      <button type="button" disabled={!runId || !trialId || !caseId} onClick={createTask}>
        创建审核任务
      </button>
      <h3>审核任务({tasks.length})</h3>
      {tasks.length === 0 ? (
        <EmptyNote text="暂无审核任务;run 完成后会按抽样策略自动生成(必审/风险/随机)。" />
      ) : (
        <table className="eval-table">
          <thead>
            <tr>
              <th>任务</th>
              <th>run</th>
              <th>用例</th>
              <th>优先级</th>
              <th>状态</th>
              <th>抽样依据</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {tasks.map((task) => (
              <tr key={task.reviewTaskId}>
                <td>
                  <code>{task.reviewTaskId}</code>
                </td>
                <td>{task.runId}</td>
                <td>{task.caseId}</td>
                <td>{task.priority}</td>
                <td>{task.status}</td>
                <td>{task.sampling.rule}</td>
                <td>
                  <button type="button" onClick={() => act(task, 'claim')}>
                    领取
                  </button>
                  <button type="button" onClick={() => act(task, 'submit')}>
                    提交意见
                  </button>
                  <button type="button" onClick={() => act(task, 'dispute')}>
                    争议
                  </button>
                  <button type="button" onClick={() => act(task, 'adjudicate')}>
                    裁决
                  </button>
                  <button
                    type="button"
                    onClick={() =>
                      setExpanded(expanded === task.reviewTaskId ? '' : task.reviewTaskId)
                    }
                  >
                    {expanded === task.reviewTaskId ? '收起详情' : '详情'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {expanded && <ReviewDetailPanel taskId={expanded} />}
    </section>
  )
}

function CompareTab() {
  const [baseline, setBaseline] = useState('')
  const [candidate, setCandidate] = useState('')
  const [result, setResult] = useState<ComparisonView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const submit = useCallback(() => {
    setError(null)
    api
      .compare(baseline, candidate)
      .then(setResult)
      .catch((exc: unknown) => setError(exc instanceof ApiError ? exc.message : String(exc)))
  }, [baseline, candidate])
  return (
    <section>
      <h2>批次比较</h2>
      <ErrorNote message={error} />
      <label>
        基线 runId
        <input value={baseline} onChange={(event) => setBaseline(event.target.value)} />
      </label>
      <label>
        候选 runId
        <input value={candidate} onChange={(event) => setCandidate(event.target.value)} />
      </label>
      <button type="button" disabled={!baseline || !candidate} onClick={submit}>
        比较
      </button>
      {result && (
        <div>
          <p
            className={
              result.comparability === 'compatible' || result.comparability === 'subject_comparison'
                ? 'eval-ok'
                : 'eval-note'
            }
          >
            {result.note}
          </p>
          {result.subjectDeltas.length > 0 && <p>被测对象差异:{result.subjectDeltas.join(';')}</p>}
          <p>
            配对 {result.coverage.pairedCount};仅基线 {result.coverage.onlyInBaseline.length};
            仅候选 {result.coverage.onlyInCandidate.length}
          </p>
          <p>
            独立统计样本:
            {result.independentSamples ? '是(live 双方)' : '否(Mock/缓存/回放不计入样本量)'}
            ;总体差值 {result.summary.overallDelta === null ? 'N/A' : result.summary.overallDelta}
          </p>
          <p className="eval-muted">{result.summary.uncertainty}</p>
          {result.checkRegressions.length > 0 && (
            <div className="eval-gaps">
              <h4>检查项级退化(通过 → 失败/依赖失败;gradeId 可回查证据)</h4>
              <table className="eval-table">
                <thead>
                  <tr>
                    <th>用例</th>
                    <th>检查项</th>
                    <th>基线</th>
                    <th>候选</th>
                    <th>候选 gradeId</th>
                  </tr>
                </thead>
                <tbody>
                  {result.checkRegressions.map((regression) => (
                    <tr key={`${regression.caseId}:${regression.checkId}`}>
                      <td>{regression.caseId}</td>
                      <td>{regression.checkId}</td>
                      <td>{regression.baselineStatus}</td>
                      <td>{regression.candidateStatus}</td>
                      <td>
                        <code>{regression.candidateGradeId}</code>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {result.candidateErrorCategories.length > 0 && (
            <div className="eval-gaps">
              <h4>候选 run 执行失败按错误类别分组(关联 trial 可查调用记录)</h4>
              <ul>
                {result.candidateErrorCategories.map((item) => (
                  <li key={item.category}>
                    <strong>{item.category}</strong>: {item.trialIds.length} 个 trial
                    {item.trialIds.slice(0, 5).map((trialId) => (
                      <code key={trialId}> {trialId.slice(0, 14)}…</code>
                    ))}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <table className="eval-table">
            <thead>
              <tr>
                <th>用例</th>
                <th>基线</th>
                <th>候选</th>
                <th>Δ</th>
              </tr>
            </thead>
            <tbody>
              {result.paired.map((pair) => (
                <tr key={`${pair.caseId}`}>
                  <td>{pair.caseId}</td>
                  <td>
                    {pair.baseline.verdict ?? pair.baseline.status}(
                    {pair.baseline.score === null ? '未评' : pair.baseline.score.toFixed(2)})
                  </td>
                  <td>
                    {pair.candidate.verdict ?? pair.candidate.status}(
                    {pair.candidate.score === null ? '未评' : pair.candidate.score.toFixed(2)})
                  </td>
                  <td>{pair.delta === null ? 'N/A' : pair.delta.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

function CostTab() {
  const [baseline, setBaseline] = useState('')
  const [compressed, setCompressed] = useState('')
  const [result, setResult] = useState<CompressionReport | null>(null)
  const [error, setError] = useState<string | null>(null)
  const submit = useCallback(() => {
    setError(null)
    api
      .compressionReport(baseline, compressed)
      .then(setResult)
      .catch((exc: unknown) => setError(exc instanceof ApiError ? exc.message : String(exc)))
  }, [baseline, compressed])
  return (
    <section>
      <h2>成本与压缩实验</h2>
      <p className="eval-note">
        配对比较:同数据集各跑一个"原始"与"压缩"run(新建评测勾选压缩),差异只来自压缩。 token
        仅统计服务商 reported usage;未知保持未知,字符数不冒充 token(§13.1)。
      </p>
      <ErrorNote message={error} />
      <label>
        基线 runId(未压缩)
        <input value={baseline} onChange={(event) => setBaseline(event.target.value)} />
      </label>
      <label>
        压缩 runId
        <input value={compressed} onChange={(event) => setCompressed(event.target.value)} />
      </label>
      <button type="button" disabled={!baseline || !compressed} onClick={submit}>
        生成压缩实验报告
      </button>
      {result && (
        <div>
          <p>
            压缩器:{result.compressorVersion ?? '—'};质量:基线{' '}
            {percent(result.quality.baselinePassRate)} → 压缩{' '}
            {percent(result.quality.compressedPassRate)}(Δ {result.quality.delta ?? 'N/A'})
          </p>
          <p>
            token(服务商口径):基线 {result.tokens.baseline.providerReportedTokens ?? '未知'} → 压缩{' '}
            {result.tokens.compressed.providerReportedTokens ?? '未知'}
            {result.tokens.delta !== null && `(Δ ${result.tokens.delta})`}
          </p>
          <p>
            费用(已结算/估算口径):基线 {money(result.cost.baselineSettled)} → 压缩{' '}
            {money(result.cost.compressedSettled)}
            {result.cost.delta !== null && `(Δ ${result.cost.delta.toFixed(4)})`}
          </p>
          <p className={result.promote ? 'eval-ok' : 'eval-error'}>
            推广结论:{result.promote === null ? '证据不足' : result.promote ? '可推广' : '禁止推广'}
            {result.promoteReason && ` — ${result.promoteReason}`}
          </p>
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
    <section>
      <h2>运营状态</h2>
      <ErrorNote message={loadError} />
      {health === null ? (
        <EmptyNote text="加载健康指标…" />
      ) : (
        <table className="eval-table">
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
        </table>
      )}
      {alerts && alerts.alerts.length > 0 && (
        <div className="eval-gaps">
          <h4>告警(仅列出,不自动发送;阈值可配置)</h4>
          <ul>
            {alerts.alerts.map((alert) => (
              <li key={alert.code}>
                <strong>{alert.code}</strong>: {alert.message}
              </li>
            ))}
          </ul>
        </div>
      )}
      <h3>观测导出 Outbox</h3>
      {outbox && (
        <p>
          积压 {outbox.pending} 条;Langfuse:{' '}
          <strong>{outbox.langfuseConfigured ? '已配置(未验证连通)' : '未配置'}</strong>
          <button type="button" onClick={flush}>
            手动导出重试
          </button>
        </p>
      )}
      {flushMessage && <p className="eval-muted">{flushMessage}</p>}
      <h3>裁判校准记录</h3>
      {calibrations === null ? (
        <EmptyNote text="加载中…" />
      ) : calibrations.length === 0 ? (
        <EmptyNote text="尚无校准记录;vlm 裁判未校准前,质量门禁保持 inconclusive(§11.3)。可通过 POST /api/evals/v1/calibrations 导入人工标注。" />
      ) : (
        <table className="eval-table">
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
        </table>
      )}
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
    const requested = new URLSearchParams(location.search).get('tab')
    return TABS.some((item) => item.id === requested) ? (requested as Tab) : 'overview'
  })
  const [pendingRun, setPendingRun] = useState<string>('')
  const openRun = useCallback((runId: string) => {
    setPendingRun(runId)
    setTab('runs')
  }, [])
  const runsTab = useMemo(() => <RunsTab key={pendingRun} initialRun={pendingRun} />, [pendingRun])
  return (
    <QueryClientProvider client={queryClient}>
      <div className="eval-app">
        <header>
          <h1>评测工作台 · 帧屿集 FRAYUNE</h1>
          <nav>
            {TABS.map((item) => (
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
        </header>
        <main>
          {tab === 'overview' && <OverviewTab onOpenRun={openRun} />}
          {tab === 'new' && <NewRunTab onCreated={openRun} />}
          {tab === 'runs' && runsTab}
          {tab === 'reviews' && <ReviewsTab />}
          {tab === 'compare' && <CompareTab />}
          {tab === 'cost' && <CostTab />}
          {tab === 'ops' && <OpsTab />}
        </main>
        <footer className="eval-muted">
          证据库为权威来源;Mock/stub 分数仅验证管线。live 需显式预算与密钥(默认
          0);未配置的集成如实标注。
        </footer>
      </div>
    </QueryClientProvider>
  )
}
