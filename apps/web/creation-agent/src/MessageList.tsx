import { useLayoutEffect, useRef, useState } from 'react'
import { useStore } from 'zustand'
import type { Part } from './model.js'
import { agentStore } from './store.js'
import { MessageBubble } from './MessageBubble.js'
export function MessageList({
  open,
}: {
  open: (part: Extract<Part, { kind: 'artifact' }>, trigger: HTMLElement) => void
}) {
  const ids = useStore(agentStore, (s) => s.messageIds)
  const messages = useStore(agentStore, (s) => s.messagesById)
  const status = useStore(agentStore, (s) => s.status)
  const scroll = useRef<HTMLDivElement>(null)
  const bottom = useRef(true)
  const [unread, setUnread] = useState(false)
  const follow = () => {
    const element = scroll.current
    if (element) {
      element.scrollTop = element.scrollHeight
    }
    bottom.current = true
    setUnread(false)
  }
  useLayoutEffect(() => {
    if (status === 'awaiting_user') {
      setUnread(false)
      return
    }
    if (bottom.current) {
      follow()
    } else {
      setUnread(true)
    }
  }, [messages, status])
  return (
    <main className="message-region">
      <div
        className="message-scroll"
        ref={scroll}
        onScroll={() => {
          const el = scroll.current
          if (el) {
            bottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
            if (bottom.current) {
              setUnread(false)
            }
          }
        }}
      >
        <div className="conversation">
          {!ids.length && (
            <div className="welcome">
              <span className="welcome-symbol">✦</span>
              <p className="eyebrow">从一个想法开始</p>
              <h1>把灵感，变成作品。</h1>
              <p>描述你想创作的画面，剩下的交给创作助手。</p>
              <div className="suggestions">
                <button onClick={() => agentStore.getState().setDraft('生成一张咖啡海报')}>
                  一张咖啡海报 ↗
                </button>
                <button
                  onClick={() => agentStore.getState().setDraft('做一条30秒咖啡广告，3个镜头')}
                >
                  一条三镜头广告 ↗
                </button>
              </div>
            </div>
          )}
          {ids.map((id) => (
            <MessageBubble key={id} message={messages[id]} open={open} />
          ))}
        </div>
      </div>
      {unread && (
        <button className="new-content" onClick={follow}>
          ↓ 有新内容
        </button>
      )}
    </main>
  )
}
