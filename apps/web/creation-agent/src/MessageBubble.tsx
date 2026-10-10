import type { Message, Part } from './model.js'
import { PartView } from './parts/PartView.js'
export function MessageBubble({
  message,
  open,
}: {
  message: Message
  open: (part: Extract<Part, { kind: 'artifact' }>, trigger: HTMLElement) => void
}) {
  let number = 0
  return (
    <article
      className={`message ${message.role}`}
      aria-label={message.role === 'user' ? '你的消息' : '助手消息'}
    >
      {message.role === 'assistant' && (
        <div className="assistant-label">
          <span className="avatar">✦</span>创作助手
        </div>
      )}
      <div className="parts">
        {message.parts.map((part, index) => {
          if (part.kind === 'artifact') {
            number++
          }
          return (
            <PartView
              key={
                part.kind === 'confirm'
                  ? part.confirmId
                  : part.kind === 'artifact'
                    ? part.id
                    : `${message.id}-${index}`
              }
              part={part}
              number={number}
              open={open}
            />
          )
        })}
      </div>
      {message.parts.length === 0 && <p className="muted">正在连接…</p>}
      {message.status === 'cancelled' && <p className="muted">已停止生成，已完成作品保留。</p>}
    </article>
  )
}
