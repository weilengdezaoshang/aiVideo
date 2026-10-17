import { EvaluationTable } from './EvaluationTable.js'
import { useState } from 'react'
import { api, type CaseDefinition, type TrialView } from './api.js'
import { CaseExplanation, caseTitle } from './CaseBrowser.js'
import { label } from './caseCopy.js'
import { ACTIVE_POLL_MS, useEvalQuery } from './queries.js'

export function trialLabel(trial: TrialView | undefined): string {
  if (!trial || trial.status === 'pending' || trial.status === 'queued') {
    return '待跑'
  }
  if (trial.status === 'running') {
    return '执行中'
  }
  if (trial.status === 'completed') {
    return trial.qualityVerdict === 'pass'
      ? '通过'
      : trial.qualityVerdict === 'fail'
        ? '判分未通过'
        : '已执行，待判分'
  }
  const states: Record<string, string> = {
    cancelled: '已取消',
    timed_out: '执行超时',
    failed: '执行失败',
    interrupted: '已中断',
  }
  return states[trial.status] ?? `未知状态：${trial.status}`
}
export function FlowRunResults({ runId, active }: { runId: string; active: boolean }) {
  const casesQuery = useEvalQuery(['eval-run-cases', runId], () => api.runCases(runId))
  const trialsQuery = useEvalQuery(
    ['eval-flow-trials', runId, active],
    () => api.trials(runId),
    active ? ACTIVE_POLL_MS : false,
  )
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [expanded, setExpanded] = useState(false)
  const [filter, setFilter] = useState('')
  const cases = casesQuery.data?.cases ?? []
  const trials = trialsQuery.data?.trials ?? []
  const byCase = new Map(trials.map((trial) => [trial.caseId, trial]))
  const layers = [
    ...new Set(cases.flatMap((item) => item.suites).filter((suite) => /^L[0-5]$/u.test(suite))),
  ].sort()
  const selected = cases.find((item) => item.caseId === selectedId)
  const error = casesQuery.error?.message ?? trialsQuery.error?.message
  const visible = cases.filter((item) => !filter || item.suites.includes(filter))
  return (
    <section className="flow-run-results" aria-label="本次用例执行情况">
      {error && (
        <p role="alert">
          读取用例执行情况失败：{error}{' '}
          <button
            type="button"
            onClick={() => {
              void casesQuery.refetch()
              void trialsQuery.refetch()
            }}
          >
            重试
          </button>
        </p>
      )}
      {casesQuery.isPending || trialsQuery.isPending ? (
        <p role="status">正在读取本次用例执行情况…</p>
      ) : (
        <>
          <div className="flow-layer-results">
            {layers.map((layer) => {
              const group = cases.filter((item) => item.suites.includes(layer))
              const statuses = group.map((item) => trialLabel(byCase.get(item.caseId)))
              const passed = statuses.filter((status) => status === '通过').length
              const pending = statuses.filter((status) => status === '待跑').length
              const running = statuses.filter((status) => status === '执行中').length
              const ended = group.length - pending - running
              const counts = [
                ['待跑', pending],
                ['执行中', running],
                ['通过', passed],
                ['待判分', statuses.filter((status) => status === '已执行，待判分').length],
                ['判分未通过', statuses.filter((status) => status === '判分未通过').length],
                ['执行失败', statuses.filter((status) => status === '执行失败').length],
                ['执行超时', statuses.filter((status) => status === '执行超时').length],
                ['已取消', statuses.filter((status) => status === '已取消').length],
                ['已中断', statuses.filter((status) => status === '已中断').length],
              ] as const
              return (
                <div key={layer}>
                  <strong>{label(layer)}</strong>
                  <p>
                    {passed === group.length
                      ? `${group.length} 道题全部通过`
                      : `已结束 ${ended} / ${group.length}`}
                  </p>
                  {passed !== group.length && (
                    <small>
                      {counts
                        .filter(([, count]) => count > 0)
                        .map(([name, count]) => `${name} ${count}`)
                        .join(' · ')}
                    </small>
                  )}
                </div>
              )
            })}
          </div>
          <details
            className="flow-case-results"
            open={expanded}
            onToggle={(event) => setExpanded(event.currentTarget.open)}
          >
            <summary>
              {expanded ? '收起' : '展开'}本次 {cases.length} 道题的{active ? '执行情况' : '结果'}
            </summary>
            <label>
              按覆盖层查看
              <select value={filter} onChange={(event) => setFilter(event.target.value)}>
                <option value="">全部覆盖层</option>
                {layers.map((layer) => (
                  <option key={layer} value={layer}>
                    {label(layer)}
                  </option>
                ))}
              </select>
            </label>
            <div className="eval-table-scroll">
              <EvaluationTable className="eval-table">
                <thead>
                  <tr>
                    <th>用例</th>
                    <th>覆盖层</th>
                    <th>当前状态</th>
                    <th>自动分</th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((item) => {
                    const trial = byCase.get(item.caseId)
                    const status = trialLabel(trial)
                    return (
                      <tr key={item.caseId}>
                        <td>
                          <button type="button" onClick={() => setSelectedId(item.caseId)}>
                            {caseTitle(item)}
                          </button>
                          <small className="flow-case-id">{item.caseId}</small>
                        </td>
                        <td>{item.suites.find((suite) => /^L[0-5]$/u.test(suite)) ?? '未标注'}</td>
                        <td
                          className={
                            status === '通过'
                              ? 'eval-status-pass'
                              : status === '判分未通过' || status === '执行失败'
                                ? 'eval-status-fail'
                                : ''
                          }
                        >
                          {status}
                        </td>
                        <td>
                          {trial?.weightedScore == null ? '未评分' : trial.weightedScore.toFixed(2)}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </EvaluationTable>
            </div>
            {selected && (
              <>
                <button type="button" onClick={() => setSelectedId(null)}>
                  收起用例说明
                </button>
                <CaseExplanation
                  key={selected.caseId}
                  item={selected}
                  trial={byCase.get(selected.caseId)}
                />
              </>
            )}
          </details>
        </>
      )}
    </section>
  )
}
export function FlowCasePreview({ cases }: { cases: CaseDefinition[] }) {
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const selected = cases.find((item) => item.caseId === selectedId)
  return (
    <details className="flow-case-preview">
      <summary>查看将要运行的 {cases.length} 道题</summary>
      <p>这里是用例清单，点击标题可查看测什么、怎样判分。</p>
      <ul>
        {cases.map((item) => (
          <li key={item.caseId}>
            <button type="button" onClick={() => setSelectedId(item.caseId)}>
              {caseTitle(item)}
            </button>
          </li>
        ))}
      </ul>
      {selected && <CaseExplanation key={selected.caseId} item={selected} />}
    </details>
  )
}
