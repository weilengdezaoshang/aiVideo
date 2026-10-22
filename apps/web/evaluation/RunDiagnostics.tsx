/** Read-only PostgreSQL run diagnostics on the authenticated application origin. */
import { useEffect, useRef, useState, type FormEvent } from 'react'

type Json = string | number | boolean | null | Json[] | { [key: string]: Json }
type Section =
  | 'messages'
  | 'plans'
  | 'tasks'
  | 'calls'
  | 'attempts'
  | 'events'
  | 'deliveries'
  | 'alerts'
  | 'quarantines'
  | 'costs'
  | 'children'
interface Span {
  id: string
  kind: string
  run_id: string
  parent_run_id: string | null
  task_id: string | null
  trace_id: string
  at: string
  data: Record<string, Json>
  links: Record<string, string>
}
interface Page {
  items: Span[]
  next: string | null
}
interface DiagnosticReport {
  run: Record<string, Json> & { id: string; status: string }
  sections: Record<Section, Page>
  invariants: Record<string, Json>[]
}
const sections: { key: Section; label: string }[] = [
  { key: 'messages', label: '用户消息与回复' },
  { key: 'plans', label: '计划版本与审核' },
  { key: 'tasks', label: '任务与依赖' },
  { key: 'calls', label: '规划调用与审核记录' },
  { key: 'attempts', label: '执行尝试' },
  { key: 'events', label: '因果事件时间线' },
  { key: 'deliveries', label: '事件消费尝试与错误' },
  { key: 'alerts', label: '关联告警' },
  { key: 'quarantines', label: '隔离记录' },
  { key: 'costs', label: '逐步花费' },
  { key: 'children', label: '子 Run' },
]
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}
function isPage(value: unknown): value is Page {
  return (
    object(value) &&
    (value.next === null || typeof value.next === 'string') &&
    Array.isArray(value.items) &&
    value.items.every(
      (span: unknown) =>
        object(span) && typeof span.id === 'string' && object(span.data) && object(span.links),
    )
  )
}
async function read(url: string, signal: AbortSignal): Promise<unknown> {
  const response = await fetch(url, { credentials: 'same-origin', signal })
  if (!response.ok) {
    throw new Error(
      response.status === 401
        ? '请先登录应用。'
        : response.status === 403
          ? '需要工作区管理员权限。'
          : response.status === 404
            ? '此工作区没有该 Run。'
            : '读取失败，请重试。',
    )
  }
  return response.json() as Promise<unknown>
}
function pretty(value: unknown): string {
  return JSON.stringify(value, null, 2)
}

export function RunDiagnostics() {
  const [input, setInput] = useState(() => new URLSearchParams(location.search).get('run_id') ?? '')
  const [report, setReport] = useState<DiagnosticReport | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [paging, setPaging] = useState<Section | null>(null)
  const controller = useRef<AbortController | null>(null)
  const generation = useRef(0)
  useEffect(() => () => controller.current?.abort(), [])
  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!uuid.test(input.trim())) {
      setError('请输入有效的 Run UUID。')
      return
    }
    controller.current?.abort()
    const active = new AbortController()
    controller.current = active
    const request = ++generation.current
    setBusy(true)
    setPaging(null)
    setError(null)
    setReport(null)
    try {
      const value = await read(
        '/api/internal/core/runs/' + encodeURIComponent(input.trim()),
        active.signal,
      )
      if (
        !object(value) ||
        !object(value.run) ||
        typeof value.run.id !== 'string' ||
        typeof value.run.status !== 'string' ||
        !object(value.sections) ||
        !sections.every(({ key }) => isPage((value.sections as Record<string, unknown>)[key])) ||
        !Array.isArray(value.invariants)
      ) {
        throw new Error('排障数据格式无效。')
      }
      // The endpoint projection is validated at this JSON boundary; no provider objects enter the UI.
      if (generation.current === request) {
        setReport(value as unknown as DiagnosticReport)
      }
    } catch (failure) {
      if (!active.signal.aborted && generation.current === request) {
        setError(failure instanceof Error ? failure.message : '读取失败。')
      }
    } finally {
      if (generation.current === request) {
        setBusy(false)
      }
    }
  }
  async function more(key: Section) {
    if (!report || !report.sections[key].next || paging) {
      return
    }
    const current = report
    const request = generation.current
    setPaging(key)
    setError(null)
    try {
      const signal = controller.current?.signal ?? new AbortController().signal
      const value = await read(
        `/api/internal/core/runs/${current.run.id}/${key}?after=${encodeURIComponent(current.sections[key].next ?? '')}`,
        signal,
      )
      if (!isPage(value)) {
        throw new Error('分页数据格式无效。')
      }
      if (request === generation.current) {
        setReport((previous) => {
          if (!previous || previous.run.id !== current.run.id) {
            return previous
          }
          const items = [...previous.sections[key].items, ...value.items]
          return {
            ...previous,
            sections: {
              ...previous.sections,
              [key]: {
                items: [...new Map(items.map((item) => [item.id, item])).values()],
                next: value.next,
              },
            },
          }
        })
      }
    } catch (failure) {
      if (request === generation.current && !controller.current?.signal.aborted) {
        setError(failure instanceof Error ? failure.message : '分页读取失败。')
      }
    } finally {
      if (request === generation.current) {
        setPaging(null)
      }
    }
  }
  return (
    <section aria-labelledby="diagnostics-title">
      <h2 id="diagnostics-title">Run 排障</h2>
      <p>读取消息、计划、审核、执行与事件的数据库记录。只读，不会重试任务或修改积分。</p>
      <form onSubmit={(event) => void submit(event)} className="eval-inline">
        <label htmlFor="diagnostic-run-id">Run ID</label>
        <input
          id="diagnostic-run-id"
          value={input}
          onChange={(event) => setInput(event.target.value)}
          autoComplete="off"
          spellCheck={false}
        />
        <button type="submit" disabled={busy}>
          {busy ? '读取中…' : '查询'}
        </button>
      </form>
      {error && (
        <p role="alert" className="eval-error">
          {error}
        </p>
      )}
      {!report && !busy && !error && <p className="eval-empty">输入 Run ID，查看运行经过。</p>}
      {report && (
        <>
          <h3>
            {report.run.id} · {report.run.status}
          </h3>
          <details open>
            <summary>运行状态与预算</summary>
            <pre>{pretty(report.run)}</pre>
          </details>
          <p role="status">
            {report.invariants.length
              ? `发现 ${report.invariants.length} 项不变量异常`
              : '当前已检查的不变量未发现异常'}
          </p>
          {report.invariants.length > 0 && <pre>{pretty(report.invariants)}</pre>}
          {sections.map(({ key, label }) => (
            <section key={key} aria-label={label}>
              <h3>{label}</h3>
              {report.sections[key].items.length === 0 && (
                <p className="eval-empty">未记录；历史数据不会补造记录。</p>
              )}
              {key === 'costs' && report.sections.costs.items.length > 0 ? (
                <table className="eval-table">
                  <thead>
                    <tr>
                      <th>Task</th>
                      <th>预扣</th>
                      <th>结算</th>
                      <th>退款</th>
                      <th>未结</th>
                      <th>状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.sections.costs.items.map((span) => (
                      <tr key={span.id}>
                        <td>{span.task_id}</td>
                        <td>{String(span.data.quoted)}</td>
                        <td>{String(span.data.charged)}</td>
                        <td>{String(span.data.refunded)}</td>
                        <td>
                          {Number(span.data.quoted) -
                            Number(span.data.charged) -
                            Number(span.data.refunded)}
                        </td>
                        <td>{String(span.data.state)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                report.sections[key].items.map((span) => (
                  <details key={span.id} id={'trace-' + span.id}>
                    <summary>
                      {String(
                        span.data.task_key ??
                          span.data.event_type ??
                          span.data.kind ??
                          span.data.role ??
                          span.data.version ??
                          span.id,
                      )}{' '}
                      · {String(span.data.status ?? span.data.verdict ?? '')}
                    </summary>
                    <p>
                      run_id: {span.run_id} · parent_run_id: {span.parent_run_id ?? '—'} · task_id:{' '}
                      {span.task_id ?? '—'}
                    </p>
                    {Object.entries(span.links).map(([kind, id]) => (
                      <p key={kind}>
                        {kind}: <a href={'#trace-' + id}>{id}</a>
                      </p>
                    ))}
                    <pre>{pretty(span.data)}</pre>
                  </details>
                ))
              )}
              {report.sections[key].next && (
                <button
                  type="button"
                  disabled={paging !== null || busy}
                  onClick={() => void more(key)}
                >
                  {paging === key ? '读取中…' : `加载更多${label}`}
                </button>
              )}
            </section>
          ))}
        </>
      )}
    </section>
  )
}
