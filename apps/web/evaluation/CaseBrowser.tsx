import { EvaluationTable } from './EvaluationTable.js'
import { useState } from 'react'
import { api, type CaseDefinition, type TrialView } from './api.js'
import { useEvalQuery } from './queries.js'
import { expectationLines, failureReason, humanValue, label, record } from './caseCopy.js'

export function caseTitle(item: CaseDefinition): string {
  return item.title?.trim() || item.input.prompt.slice(0, 40) || `${label(item.taskType)}用例`
}
function stepsFor(item: CaseDefinition): string[] {
  if (item.taskType !== 'agent_flow') {
    return [`发送：“${item.input.prompt}”`]
  }
  const steps = item.steps ?? item.input.params.steps
  if (!Array.isArray(steps)) {
    return [`发送：“${item.input.prompt}”`]
  }
  return steps.map((raw: unknown) => {
    const step = record(raw)
    const actions: Record<string, string> = {
      send: `发送：“${String(step.prompt ?? '')}”`,
      resume: step.approved ? '批准积分确认，继续生成' : '取消确认',
      stop: '生成中停止',
      invalid: '发送非法请求，检查错误响应',
      resume_unknown: '确认不存在的轮次',
      stop_unknown: '停止不存在的轮次',
      queue: `测试队列：${label(String(step.probe))}`,
    }
    const known = ['action', 'prompt', 'approved', 'body', 'when', 'probe']
    const extras = Object.entries(step)
      .filter(([key]) => !known.includes(key))
      .map(([key, value]) => `${label(key)}：${humanValue(value)}`)
    return [
      actions[String(step.action)] ?? `${label(String(step.action))}：${humanValue(step)}`,
      ...extras,
    ].join('；')
  })
}
export function CaseExplanation({ item, trial }: { item: CaseDefinition; trial?: TrialView }) {
  const expect = item.expect ?? record(item.input.params.expect)
  const mocks = item.mocks ?? record(item.input.params.mocks)
  const flow = item.taskType === 'agent_flow'
  const layer = item.suites.find((suite) => /^L[0-5]$/u.test(suite))
  const description =
    item.description ||
    (item.title
      ? `验证“${item.title}”是否按预期工作。${layer ? (layerDescriptions[layer] ?? '') : ''}`
      : `验证${layer ? label(layer) : label(item.taskType)}的行为，以下条件决定是否通过。`)
  const mockLabels: Record<string, string> = {
    imageProvider: '生图模型',
    toolDelayMs: '模拟工具延迟（毫秒）',
    imageCredits: '每次生图模拟积分',
    creditThreshold: '积分确认阈值',
  }
  return (
    <article className="eval-case-explanation" aria-label={`用例说明：${caseTitle(item)}`}>
      <h3>{caseTitle(item)}</h3>
      <p className="eval-case-meta">
        用例编号：<code>{item.caseId}</code> · {label(item.taskType)} ·{' '}
        {item.suites.map(label).join(' / ')}
      </p>
      <h4>这道题在测什么</h4>
      <p>{description}</p>
      <h4>用户会做什么</h4>
      <ol className="eval-case-timeline">
        {stepsFor(item).map((step, index) => (
          <li key={index}>{step}</li>
        ))}
      </ol>
      <h4>怎样算通过</h4>
      {flow && Object.keys(expect).length > 0 ? (
        <ul>
          {Object.entries(expect).flatMap(([key, value]) =>
            expectationLines({ [key]: value }).map((line, index) => (
              <li key={`${key}-${index}`}>
                {line} <small className="eval-case-code">（{key}）</small>
              </li>
            )),
          )}
        </ul>
      ) : (
        <>
          <p>执行完成且所有必需的判分问题满足预期，才算通过。</p>
          <h5>判分问题</h5>
          <ul>
            {item.checks.map((check) => (
              <li key={check.id}>
                {check.question} — 预期：{humanValue(check.expected)}（
                {check.required ? '必须通过' : '辅助评分'}，权重 {check.weight}）
                {check.dependsOn?.length
                  ? `；依赖：${check.dependsOn.map((id) => item.checks.find((other) => other.id === id)?.question ?? label(id)).join('、')}`
                  : ''}
              </li>
            ))}
          </ul>
        </>
      )}
      <h4>用了什么假数据</h4>
      <p>
        {Object.keys(mocks).length
          ? Object.entries(mocks)
              .map(([key, value]) => `${mockLabels[key] ?? label(key)}：${humanValue(value)}`)
              .join('；')
          : '用例未指定假数据；新建评测的模拟模式使用模拟生成和裁判，真实模式以运行配置为准。'}
      </p>
      {trial && (
        <section aria-label="本条自动评分">
          <h4>本条自动评分</h4>
          <p>自动分：{trial.weightedScore ?? '未评分'}</p>
          {trial.error && (
            <p role="alert">
              {failureReason(trial.error)} <small>（原码：{trial.error}）</small>
            </p>
          )}
          <ul>
            {trial.grades
              .filter((grade) => grade.status !== 'pass')
              .map((grade) => (
                <li key={grade.gradeId}>
                  {failureReason(
                    grade.checkId,
                    item.checks.find((check) => check.id === grade.checkId)?.question,
                  )}{' '}
                  <small>
                    （原码：{grade.checkId}；{grade.status === 'fail' ? '失败' : '无法判定'}）
                  </small>
                </li>
              ))}
          </ul>
        </section>
      )}
      <details>
        <summary>查看原始 JSON</summary>
        <pre className="eval-payload">{JSON.stringify(item, null, 2)}</pre>
      </details>
    </article>
  )
}
export function CaseList({
  cases,
  initialCaseId,
}: {
  cases: CaseDefinition[]
  initialCaseId?: string
}) {
  const [search, setSearch] = useState('')
  const [suite, setSuite] = useState(
    initialCaseId
      ? (cases
          .find((item) => item.caseId === initialCaseId)
          ?.suites.find((id) => /^L[0-5]$/u.test(id)) ?? '')
      : '',
  )
  const [selected, setSelected] = useState<string | null>(initialCaseId ?? null)
  const suites = [...new Set(cases.flatMap((item) => item.suites))]
  const filtered = cases.filter(
    (item) =>
      (!suite || item.suites.includes(suite)) &&
      `${item.caseId} ${caseTitle(item)} ${item.input.prompt} ${(item.tags ?? []).join(' ')}`
        .toLowerCase()
        .includes(search.toLowerCase()),
  )
  const current = cases.find((item) => item.caseId === selected)
  return (
    <div className="eval-case-browser">
      {cases.some((item) => item.taskType === 'agent_flow') && (
        <div className="eval-layer-tabs" role="group" aria-label="Harness 层级">
          {['', ...harnessLayers].map((id) => (
            <button
              key={id}
              type="button"
              aria-pressed={suite === id}
              onClick={() => {
                setSuite(id)
                setSelected(null)
              }}
            >
              {id ? layerTitles[id] : '整包'} ·{' '}
              {cases.filter((item) => !id || item.suites.includes(id)).length} 题
            </button>
          ))}
        </div>
      )}
      <div className="eval-case-filters">
        <label>
          搜索用例
          <input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="标题、提示词或用例编号"
          />
        </label>
        <label>
          覆盖层 / 测试套件
          <select value={suite} onChange={(event) => setSuite(event.target.value)}>
            <option value="">全部</option>
            {suites.map((id) => (
              <option key={id} value={id}>
                {label(id)}
              </option>
            ))}
          </select>
        </label>
      </div>
      <p>共 {filtered.length} 条用例</p>
      <div className="eval-case-table">
        <EvaluationTable className="eval-table">
          <thead>
            <tr>
              <th>用例编号</th>
              <th>中文标题</th>
              <th>覆盖层 / 套件</th>
              <th>标签</th>
              <th>题型</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((item) => (
              <tr key={item.caseId} className={selected === item.caseId ? 'eval-selected' : ''}>
                <td>
                  <code>{item.caseId}</code>
                </td>
                <td>
                  <button
                    type="button"
                    className="eval-link"
                    onClick={() => setSelected(item.caseId)}
                  >
                    {caseTitle(item)}
                  </button>
                </td>
                <td>{item.suites.map(label).join(' / ')}</td>
                <td>{(item.tags ?? []).map(label).join('、') || '—'}</td>
                <td>{label(item.taskType)}</td>
              </tr>
            ))}
          </tbody>
        </EvaluationTable>
      </div>
      {!filtered.length && <p>没有匹配的用例，请调整搜索或筛选。</p>}
      {current && (
        <aside className="eval-case-detail" aria-label="用例定义">
          <button type="button" onClick={() => setSelected(null)}>
            关闭说明
          </button>
          <CaseExplanation key={current.caseId} item={current} />
        </aside>
      )}
    </div>
  )
}
export function DatasetCases({
  datasetId,
  version,
  preview = false,
}: {
  datasetId: string
  version?: number
  preview?: boolean
}) {
  const query = useEvalQuery(['eval-cases', datasetId, version ?? 'latest'], () =>
    api.datasetCases(datasetId, version),
  )
  const [all, setAll] = useState(false)
  if (query.isPending) {
    return <p role="status">正在加载用例定义…</p>
  }
  if (query.error) {
    return (
      <div role="alert">
        无法加载用例定义：{query.error.message}{' '}
        <button type="button" onClick={() => void query.refetch()}>
          重试
        </button>
      </div>
    )
  }
  const cases = query.data?.cases ?? []
  if (!cases.length) {
    return <p>这个数据集暂无用例。</p>
  }
  const layers = [
    ...new Set(cases.flatMap((item) => item.suites).filter((suite) => /^L[0-5]$/u.test(suite))),
  ].sort()
  return (
    <section aria-label="数据集用例">
      <h3>将运行哪些题</h3>
      <p>
        {cases.length} 道题 · 覆盖层：
        {layers.length ? layers.map(label).join('、') : '未标注 L0～L5、L7（文生图判分）'}
      </p>
      {preview && !all ? (
        <>
          <ol>
            {cases.slice(0, 5).map((item) => (
              <li key={item.caseId}>{caseTitle(item)}</li>
            ))}
          </ol>
          <button type="button" onClick={() => setAll(true)}>
            查看全部用例
          </button>
        </>
      ) : (
        <CaseList cases={cases} />
      )}
    </section>
  )
}
export function RunCaseDefinition({ runId, trial }: { runId: string; trial: TrialView }) {
  const query = useEvalQuery(['eval-run-cases', runId], () => api.runCases(runId))
  if (query.isPending) {
    return <p role="status">正在加载运行时的用例定义…</p>
  }
  if (query.error) {
    return <p role="alert">无法加载运行时的用例定义：{query.error.message}</p>
  }
  const item = query.data?.cases.find((entry) => entry.caseId === trial.caseId)
  return item ? (
    <>
      <CaseExplanation key={item.caseId} item={item} trial={trial} />
      {item.taskType === 'agent_flow' && (
        <details>
          <summary>查看该层用例</summary>
          <CaseList cases={query.data?.cases ?? []} initialCaseId={item.caseId} />
        </details>
      )}
    </>
  ) : (
    <p>此运行快照没有本条用例定义。</p>
  )
}

export const harnessLayers = ['L0', 'L1', 'L2', 'L3', 'L4', 'L5', 'L7']
export const layerTitles: Record<string, string> = {
  L0: 'L0 协议',
  L1: 'L1 路由',
  L2: 'L2 计划',
  L3: 'L3 中断',
  L4: 'L4 续跑',
  L5: 'L5 队列',
  L7: 'L7 反思',
}
export function AgentSourceCases({
  selection,
  onSelection,
}: {
  selection?: string[]
  onSelection?: (layers: string[]) => void
}) {
  const query = useEvalQuery(['eval-flow-source-cases'], () => api.flowCases())
  if (query.isPending) {
    return <p role="status">正在读取 Agent 题库…</p>
  }
  if (query.error) {
    return (
      <p role="alert">
        无法读取题库：{query.error.message}
        <button onClick={() => void query.refetch()}>重试</button>
      </p>
    )
  }
  const cases = query.data?.cases ?? []
  const selected = selection ?? harnessLayers
  const filtered = cases.filter((item) => item.suites.some((id) => selected.includes(id)))
  return (
    <section aria-label="Agent 流程源题库">
      <h3>Agent 流程 · Harness 题库</h3>
      <p>直接读取 agent-flow-l0～l5 与 l7 七份 JSONL，共 {cases.length} 题；与磁盘源文件一致。</p>
      {onSelection ? (
        <>
          <fieldset>
            <legend>选择评测层级</legend>
            <label>
              <input
                type="checkbox"
                checked={harnessLayers.every((layer) => selected.includes(layer))}
                onChange={(event) => onSelection(event.target.checked ? harnessLayers : [])}
              />
              整包 agent_flow
            </label>
            {harnessLayers.map((id) => (
              <label key={id}>
                <input
                  type="checkbox"
                  checked={selected.includes(id)}
                  onChange={(event) =>
                    onSelection(
                      event.target.checked
                        ? [...selected, id]
                        : selected.filter((layer) => layer !== id),
                    )
                  }
                />
                {layerTitles[id]} · {cases.filter((item) => item.suites.includes(id)).length} 题
              </label>
            ))}
          </fieldset>
          <p>将运行 {filtered.length} 道题（本地模拟模型）</p>
          <ol>
            {filtered.map((item) => (
              <li key={item.caseId}>{caseTitle(item)}</li>
            ))}
          </ol>
          {!selected.length && <p role="alert">请至少选择一层。</p>}
        </>
      ) : (
        <CaseList cases={cases} />
      )}
    </section>
  )
}

const layerDescriptions: Record<string, string> = {
  L0: '检查事件协议、错误响应与追踪信息。',
  L1: '检查需求应直接出图还是交给策划。',
  L2: '检查计划生成、推进与完成是否一致。',
  L3: '检查确认、取消和停止后的行为。',
  L4: '检查跨轮继续能否恢复计划并保留已完成步骤。',
  L5: '检查排队、并发、限流与重复请求处理。',
  L7: '检查审核、自动重试与超限说明。',
}

export function useRunDefinitions(runId: string | undefined) {
  return useEvalQuery(
    ['eval-run-cases', runId ?? ''],
    () => api.runCases(runId!),
    false,
    Boolean(runId),
  )
}
export function CaseIdentity({ item, caseId }: { item?: CaseDefinition; caseId: string }) {
  return (
    <span className="eval-case-identity">
      <span>{item ? caseTitle(item) : caseId}</span>
      {item && <small>{caseId}</small>}
    </span>
  )
}
export function RunCaseIdentity({ runId, caseId }: { runId: string; caseId: string }) {
  const query = useRunDefinitions(runId)
  return (
    <CaseIdentity
      item={query.data?.cases?.find((entry) => entry.caseId === caseId)}
      caseId={caseId}
    />
  )
}
export function RunCheckLabel({
  runId,
  caseId,
  checkId,
}: {
  runId: string
  caseId: string
  checkId: string
}) {
  const query = useRunDefinitions(runId)
  const question = query.data?.cases
    ?.find((entry) => entry.caseId === caseId)
    ?.checks.find((check) => check.id === checkId)?.question
  return (
    <span className="eval-check-label">
      {question ? `判分问题：${question}` : failureReason(checkId)}
      <small>（{checkId}）</small>
    </span>
  )
}
