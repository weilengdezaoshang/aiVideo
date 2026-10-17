import { useEffect, useRef } from 'react'
import { useStore } from 'zustand'
import { agentStore } from './store.js'
export function Composer() {
  const draft = useStore(agentStore, (s) => s.draft)
  const status = useStore(agentStore, (s) => s.status)
  const input = useRef<HTMLTextAreaElement>(null)
  const composing = useRef(false)
  useEffect(() => {
    if (status === 'idle') {
      input.current?.focus()
    }
  }, [status])
  const busy = status !== 'idle'
  return (
    <footer className="composer-area">
      <div className="composer">
        <textarea
          ref={input}
          aria-label="创作需求"
          title="Enter 发送，Shift + Enter 换行"
          placeholder={busy ? '助手正在处理，请等待或停止…' : '描述你的创作需求…'}
          value={draft}
          disabled={busy}
          onChange={(e) => agentStore.getState().setDraft(e.target.value)}
          onCompositionStart={() => {
            composing.current = true
          }}
          onCompositionEnd={() => {
            composing.current = false
          }}
          onKeyDown={(e) => {
            if (
              e.key === 'Enter' &&
              !e.shiftKey &&
              !composing.current &&
              !e.nativeEvent.isComposing &&
              e.keyCode !== 229
            ) {
              e.preventDefault()
              agentStore.getState().send()
            }
          }}
        />
        <div className="composer-toolbar">
          {busy ? (
            <button onClick={() => agentStore.getState().stop()}>
              ■ {status === 'sending' ? '取消连接' : '停止'}
            </button>
          ) : (
            <button
              className="primary"
              disabled={!draft.trim()}
              onClick={() => agentStore.getState().send()}
            >
              发送 ↑
            </button>
          )}
        </div>
      </div>
    </footer>
  )
}
