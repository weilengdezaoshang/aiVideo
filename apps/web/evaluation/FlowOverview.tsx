import { EvaluationTable } from './EvaluationTable.js'
import { useState } from 'react'
import { RunLaunchStatus, useRunLaunch } from './RunLaunchStatus.js'
import { FlowCasePreview, FlowRunResults } from './FlowRunResults.js'
import { api } from './api.js'
import { LIST_POLL_MS, useEvalQuery } from './queries.js'

const layers = [
  ['L0', '协议与事件'],
  ['L1', '任务路由'],
  ['L2', '计划推进'],
  ['L3', '确认与取消'],
  ['L4', '跨轮继续'],
  ['L5', '排队与限流'],
  ['L7', '生成后反思'],
] as const
const statuses: Record<string, string> = {
  completed: '已完成',
  running: '运行中',
  queued: '排队中',
  stopping: '取消中',
  failed: '执行失败',
  cancelled: '已取消',
  interrupted: '已中断',
  budget_exhausted: '预算中止',
}
export function FlowOverview({
  onOpenRun,
  onQuality,
  onReviews,
}: {
  onOpenRun: (id: string) => void
  onQuality: () => void
  onReviews?: (runId: string) => void
}) {
  const launch = useRunLaunch()
  const [suite, setSuite] = useState('agent_flow')
  const [submittedSuite, setSubmittedSuite] = useState('agent_flow')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const runs = useEvalQuery(['eval-runs'], () => api.runs(), LIST_POLL_MS)
  const preview = useEvalQuery(['eval-flow-preview'], () => api.flowCases())
  const allCases = preview.data?.cases ?? []
  const planned = allCases.filter((item) => suite === 'agent_flow' || item.suites.includes(suite))
  const recentRuns = [...(runs.data?.runs ?? [])].sort((a, b) =>
    b.createdAt.localeCompare(a.createdAt),
  )
  const locked = busy || launch.active
  const suiteName = (id: string) =>
    id === 'agent_flow'
      ? '全部 L0～L5、L7'
      : `${id} ${layers.find(([layer]) => layer === id)?.[1] ?? ''}`
  async function start() {
    if (locked) {
      return
    }
    setBusy(true)
    setError('')
    try {
      const result = await api.startFlow(suite)
      setSubmittedSuite(suite)
      launch.recordRun(result.runId)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '启动失败')
    } finally {
      setBusy(false)
    }
  }
  return (
    <section className="flow-overview">
      <div className="flow-heading">
        <h1>Agent 流程评测</h1>
        <span className="environment-pill">本地 Mock · 流程规则判分</span>
      </div>
      <div className="flow-panel flow-config-panel">
        <h2>{launch.runId && !launch.active ? '再次评测' : '评测范围'}</h2>
        <div className="flow-tabs">
          <button className="selected" type="button">
            Agent 流程
          </button>
          <button type="button" onClick={onQuality}>
            图像质量
          </button>
        </div>
        <div className="suite-select">
          <label htmlFor="flow-suite">评测套件</label>
          <select
            id="flow-suite"
            disabled={locked}
            value={suite}
            onChange={(event) => setSuite(event.target.value)}
          >
            <option value="agent_flow">全部 L0～L5、L7</option>
            {layers.map(([id, name]) => (
              <option key={id} value={id}>
                {id} {name}
              </option>
            ))}
          </select>
        </div>
        <div className="layer-grid">
          {layers.map(([id, name]) => (
            <button
              key={id}
              type="button"
              disabled={locked}
              aria-pressed={suite === id || suite === 'agent_flow'}
              onClick={() => setSuite(id)}
            >
              <b>
                {id} {name}
              </b>
              <span>
                {preview.isPending
                  ? '读取用例…'
                  : `${allCases.filter((item) => item.suites.includes(id)).length} 道题`}
              </span>
            </button>
          ))}
        </div>
        {preview.error ? (
          <p role="alert">
            无法读取评测范围：{preview.error.message}{' '}
            <button type="button" onClick={() => void preview.refetch()}>
              重试
            </button>
          </p>
        ) : (
          <p className="flow-scope-description">
            {suiteName(suite)} · {preview.isPending ? '正在读取题量…' : `${planned.length} 道题`}
            。检查 Agent 的处理流程，生图使用模拟模型。
          </p>
        )}
        <FlowCasePreview cases={planned} />
        <div className="flow-start-actions">
          <button
            className="eval-primary"
            type="button"
            disabled={locked || preview.isPending || Boolean(preview.error) || planned.length === 0}
            onClick={() => void start()}
          >
            {busy
              ? '正在启动…'
              : launch.active
                ? '本次评测进行中…'
                : suite === 'agent_flow'
                  ? '运行整包'
                  : `运行 ${suite}`}
          </button>
          {suite !== 'agent_flow' && (
            <button type="button" disabled={locked} onClick={() => setSuite('agent_flow')}>
              选择整包
            </button>
          )}
        </div>
        {error && (
          <p className="eval-error" role="alert">
            {error}
          </p>
        )}
      </div>
      <section className="flow-panel flow-current-run" aria-label="本次评测工作区">
        <h2>本次评测{launch.runId ? ` · ${suiteName(submittedSuite)}` : ''}</h2>
        {launch.runId ? (
          <>
            <RunLaunchStatus launch={launch} onReviews={onReviews} />
            <FlowRunResults key={launch.runId} runId={launch.runId} active={launch.active} />
          </>
        ) : (
          <p className="eval-muted">点击运行后，在这里查看进度和结果。</p>
        )}
      </section>
      <details className="flow-panel flow-history">
        <summary>历史运行记录</summary>
        {runs.error && <p role="alert">历史记录读取失败：{runs.error.message}</p>}
        <div className="eval-table-scroll">
          <EvaluationTable className="eval-table">
            <thead>
              <tr>
                <th>运行编号</th>
                <th>状态</th>
                <th>题量</th>
                <th>开始时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {recentRuns.slice(0, 6).map((run) => (
                <tr key={run.runId}>
                  <td>
                    <code>{run.runId}</code>
                  </td>
                  <td>{statuses[run.status ?? ''] ?? '未知状态'}</td>
                  <td>{run.planCount}</td>
                  <td>{new Date(run.createdAt).toLocaleString('zh-CN')}</td>
                  <td>
                    <button type="button" onClick={() => onOpenRun(run.runId)}>
                      打开历史详情
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </EvaluationTable>
        </div>
        {!recentRuns.length && (
          <p className="eval-muted">{runs.isPending ? '加载历史记录…' : '暂无历史记录'}</p>
        )}
      </details>
    </section>
  )
}
