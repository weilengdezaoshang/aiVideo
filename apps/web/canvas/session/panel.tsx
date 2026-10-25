import { useState, useSyncExternalStore } from 'react'
import type { RunCosts, SessionRun, SessionTask, createSessionStore } from './store.js'

const statuses: Record<string, string> = {
  created: '等待规划',
  planning: '正在规划',
  awaiting_confirm: '等待确认',
  running: '执行中',
  paused: '等待处理',
  completed: '已完成',
  failed: '执行失败',
  cancelled: '已取消',
}

const taskStatuses: Record<string, string> = {
  pending: '排队中',
  validated: '已审核',
  queued: '已入队',
  submitted: '执行中',
  waiting_user: '等你操作',
  succeeded: '已完成',
  failed: '失败',
  blocked_by_model: '被拦截',
  cancelled: '已取消',
  rejected: '被退回',
}

function QuestionCard({
  task,
  store,
}: {
  task: SessionTask
  store: ReturnType<typeof createSessionStore>
}) {
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      (task.question?.form?.fields ?? []).map((field) => [
        field.id ?? field.name ?? '',
        field.default == null ? '' : String(field.default),
      ]),
    ),
  )
  const [busy, setBusy] = useState(false)
  const fields = task.question?.form?.fields ?? []
  const submitForm = () => {
    setBusy(true)
    void store
      .answer(task.id, undefined, values)
      .catch(() => undefined)
      .finally(() => setBusy(false))
  }
  return (
    <section className="next-question-card" aria-label="需要你操作">
      {task.question?.question && <p>{task.question.question}</p>}
      {task.question?.options?.map((option) => (
        <button
          key={option.id}
          type="button"
          disabled={busy}
          onClick={() => {
            setBusy(true)
            void store
              .answer(task.id, option.id)
              .catch(() => undefined)
              .finally(() => setBusy(false))
          }}
        >
          {option.label}
        </button>
      ))}
      {fields.length > 0 && (
        <form
          onSubmit={(event) => {
            event.preventDefault()
            submitForm()
          }}
        >
          {fields.map((field) => {
            const key = field.id ?? field.name ?? ''
            return (
              <label key={key}>
                {field.label || key}
                {field.required ? '（必填）' : ''}
                {field.type === 'boolean' ? (
                  <input
                    type="checkbox"
                    checked={values[key] === 'true'}
                    onChange={(event) =>
                      setValues({ ...values, [key]: String(event.target.checked) })
                    }
                  />
                ) : field.type === 'choice' ? (
                  <select
                    value={values[key]}
                    onChange={(event) => setValues({ ...values, [key]: event.target.value })}
                  >
                    {(field.choices ?? []).map((choice) => (
                      <option key={choice} value={choice}>
                        {choice}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    type={field.type === 'number' ? 'number' : 'text'}
                    value={values[key]}
                    onChange={(event) => setValues({ ...values, [key]: event.target.value })}
                  />
                )}
              </label>
            )
          })}
          <button type="submit" disabled={busy}>
            提交
          </button>
        </form>
      )}
    </section>
  )
}

function CostsCard({
  runId,
  store,
}: {
  runId: string
  store: ReturnType<typeof createSessionStore>
}) {
  const [rows, setRows] = useState<RunCosts | null>(null)
  return (
    <details className="next-costs-card">
      <summary
        onClick={() => {
          if (!rows) {
            void store
              .costs(runId)
              .then(setRows)
              .catch(() => setRows(null))
          }
        }}
      >
        花费明细{rows ? `（共 ${rows.credits_spent} 积分）` : ''}
      </summary>
      {rows && (
        <table>
          <thead>
            <tr>
              <th>步骤</th>
              <th>预估</th>
              <th>实扣</th>
              <th>已退</th>
              <th>结算</th>
            </tr>
          </thead>
          <tbody>
            {rows.tasks.map((line) => (
              <tr key={line.task_id}>
                <td>{line.step}</td>
                <td>{line.reserved}</td>
                <td>{line.actual}</td>
                <td>{line.refunded}</td>
                <td>{line.settlement}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </details>
  )
}

function RunCard({
  run,
  store,
}: {
  run: SessionRun
  store: ReturnType<typeof createSessionStore>
}) {
  const [extra, setExtra] = useState('')
  const [busy, setBusy] = useState(false)
  const overBudget = run.status === 'paused' && run.pause_reason === 'over_budget'
  const waiting = (run.tasks ?? []).filter((task) => task.status === 'waiting_user')
  return (
    <section className="next-run-card" aria-label="运行控制">
      <p role="status">
        {statuses[run.status] ?? run.status}
        {run.queue?.jobs_ahead ? ` · 前面还有 ${run.queue.jobs_ahead} 个任务` : ''}
        {run.queue?.estimated_seconds
          ? ` · 预计等待约 ${Math.ceil(run.queue.estimated_seconds / 60)} 分钟`
          : ''}
        {run.credits_limit !== undefined
          ? ` · 已用 ${run.credits_spent ?? 0}/${run.credits_limit} 积分`
          : ''}
      </p>
      {overBudget && (
        <p className="next-budget-actions">
          <input
            type="number"
            min="1"
            step="1"
            value={extra}
            aria-label="追加积分"
            onChange={(event) => setExtra(event.target.value)}
          />
          <button
            type="button"
            disabled={busy || !Number.isSafeInteger(Number(extra)) || Number(extra) < 1}
            onClick={() => {
              setBusy(true)
              void store
                .budget(run.id, 'extend', Number(extra))
                .catch(() => undefined)
                .finally(() => setBusy(false))
            }}
          >
            追加积分并继续
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              setBusy(true)
              void store
                .budget(run.id, 'stop')
                .catch(() => undefined)
                .finally(() => setBusy(false))
            }}
          >
            结束并退还
          </button>
        </p>
      )}
      {(run.tasks ?? []).length > 0 && (
        <ul className="next-task-list">
          {(run.tasks ?? []).map((task) => (
            <li key={task.id}>
              {task.key} · {taskStatuses[task.status] ?? task.status}
              {task.tool ? ` · ${task.tool}` : ''}
              {task.quoted ? ` · 预估 ${task.quoted}` : ''}
              {task.actual ? ` · 实扣 ${task.actual}` : ''}
            </li>
          ))}
        </ul>
      )}
      {waiting.map((task) => (
        <QuestionCard key={task.id} task={task} store={store} />
      ))}
      <CostsCard runId={run.id} store={store} />
      <button
        type="button"
        disabled={busy || ['completed', 'failed', 'cancelled'].includes(run.status)}
        onClick={() => {
          setBusy(true)
          void store
            .cancelRun(run.id)
            .catch(() => undefined)
            .finally(() => setBusy(false))
        }}
      >
        停止本次运行
      </button>
    </section>
  )
}

export function SessionPanel({
  store,
  selectedAssets,
  selectedNodes,
}: {
  store: ReturnType<typeof createSessionStore>
  selectedAssets: () => readonly string[]
  selectedNodes?: () => readonly string[]
}) {
  const state = useSyncExternalStore(store.subscribe, store.getSnapshot)
  const [draft, setDraft] = useState('')
  const [budget, setBudget] = useState('0')
  const [localError, setLocalError] = useState('')
  const activeRun = state.snapshot?.runs.find(
    (run) => !['completed', 'failed', 'cancelled'].includes(run.status),
  )
  const creditBudget = activeRun?.credits_limit ?? Number(budget)
  const send = () => {
    if (
      !state.snapshot ||
      !draft.trim() ||
      !Number.isSafeInteger(creditBudget) ||
      creditBudget < 0
    ) {
      return
    }
    // A message may be added while another is in flight: intake owns merging and interruption.
    try {
      void store
        .send(draft, selectedAssets(), creditBudget, selectedNodes?.() ?? [])
        .catch((error: unknown) => setLocalError(String(error)))
      setDraft('')
      setLocalError('')
    } catch (error) {
      setLocalError(String(error))
    }
  }
  return (
    <section className="next-panel-body next-chat-panel next-agent-panel" aria-label="画布会话">
      {!state.connected && <p role="status">正在同步会话…</p>}
      {localError && <p role="alert">{localError}</p>}
      {state.error && (
        <p role="alert">
          {state.error}
          <button
            type="button"
            onClick={() => {
              void store.retry()
            }}
          >
            重试同步
          </button>
        </p>
      )}
      {state.snapshot?.summary && (
        <details>
          <summary>之前的对话</summary>
          <p>{state.snapshot.summary}</p>
        </details>
      )}
      <div className="next-chat-list" aria-live="polite">
        {state.snapshot?.messages.map((message) => (
          <p key={message.id} className={`is-${message.role}`}>
            {message.content.text}
          </p>
        ))}
        {state.pending.map((message) => (
          <p key={message.client_message_id} className="is-user">
            {message.message} · 等待发送
          </p>
        ))}
        {state.snapshot?.runs.map((run) => (
          <RunCard key={run.id} run={run} store={store} />
        ))}
        {state.snapshot?.notifications?.map((notice) => (
          <p key={notice.id} role="status">
            任务{statuses[notice.body.status ?? 'completed'] ?? '已结束'}
            <button
              type="button"
              onClick={() => {
                void store.markRead(notice.id)
              }}
            >
              已读
            </button>
          </p>
        ))}
      </div>
      <form
        onSubmit={(event) => {
          event.preventDefault()
          send()
        }}
      >
        <label>
          告诉助手你想做什么
          <textarea value={draft} onChange={(event) => setDraft(event.target.value)} />
        </label>
        <label>
          本轮积分预算
          <input
            type="number"
            min="0"
            step="1"
            value={activeRun?.credits_limit ?? budget}
            disabled={activeRun?.credits_limit !== undefined}
            onChange={(event) => setBudget(event.target.value)}
          />
        </label>
        <button type="submit" disabled={!state.snapshot || !draft.trim()}>
          发送
        </button>
      </form>
    </section>
  )
}
