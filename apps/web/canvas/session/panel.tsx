import { useState, useSyncExternalStore } from 'react'
import type { createSessionStore } from './store.js'

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
          <p key={run.id} role="status">
            {statuses[run.status] ?? run.status}
          </p>
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
