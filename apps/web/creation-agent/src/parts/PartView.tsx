import { useEffect, useRef, useState } from 'react'
import { useStore } from 'zustand'
import type { Part } from '../model.js'
import { agentStore } from '../store.js'
export function StatusIcon({ state }: { state: string }) {
  return state === 'loading' || state === 'in_progress' ? (
    <span className="spinner" aria-label="进行中" />
  ) : (
    <span aria-hidden="true">
      {state === 'completed' ? '✓' : state === 'pending' ? '○' : state === 'failed' ? '!' : '−'}
    </span>
  )
}
export function ConfirmPart({ part }: { part: Extract<Part, { kind: 'confirm' }> }) {
  const busy = useStore(agentStore, (s) => s.confirming)
  const button = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    if (part.state === 'pending') {
      button.current?.scrollIntoView({ block: 'center' })
      button.current?.focus({ preventScroll: true })
    }
  }, [part.confirmId, part.state])
  const labels = {
    accepted: '已确认，继续执行',
    rejected: '已取消',
    expired: '已停止',
    pending: '等待你的确认',
  }
  return (
    <section className={`card confirmation ${part.state}`} aria-label="积分确认">
      <div className="card-heading">
        <span>✦ 积分确认</span>
        <span className="credit">{part.estimateCredits} 积分</span>
      </div>
      <h3>{part.title}</h3>
      <p>{part.summary}</p>
      {part.state === 'pending' ? (
        <div className="actions">
          <button
            ref={button}
            className="primary"
            disabled={busy}
            onClick={() => void agentStore.getState().confirm(true)}
          >
            {busy ? '正在确认…' : '确认'}
          </button>
          <button disabled={busy} onClick={() => void agentStore.getState().confirm(false)}>
            取消
          </button>
        </div>
      ) : (
        <p className="accepted-label">{labels[part.state]}</p>
      )}
    </section>
  )
}
export function ArtifactPart({
  part,
  number,
  open,
}: {
  part: Extract<Part, { kind: 'artifact' }>
  number: number
  open: (part: Extract<Part, { kind: 'artifact' }>, trigger: HTMLElement) => void
}) {
  const [failed, setFailed] = useState(false)
  return (
    <div className="artifact">
      {failed || part.status === 'failed' ? (
        <div className="image-error">作品加载失败</div>
      ) : (
        <button
          className="image-button"
          aria-label={`放大作品 ${number}`}
          onClick={(event) => open(part, event.currentTarget)}
        >
          {part.type === 'image' ? (
            <img src={part.url} alt={`咖啡创作作品 ${number}`} onError={() => setFailed(true)} />
          ) : (
            <video src={part.url} controls />
          )}
        </button>
      )}
      <div className="artifact-caption">
        <span>作品 {String(number).padStart(2, '0')}</span>
        <span>点击放大 ↗</span>
      </div>
    </div>
  )
}
export function PartView({
  part,
  number,
  open,
}: {
  part: Part
  number: number
  open: (part: Extract<Part, { kind: 'artifact' }>, trigger: HTMLElement) => void
}) {
  switch (part.kind) {
    case 'text':
      return <div className="message-text">{part.text}</div>
    case 'tool_status':
      return (
        <div className={`tool ${part.state}`}>
          <StatusIcon state={part.state} />
          <span>
            {(
              {
                creative_agent_mcp_gen_text2image_v3: '生图',
                handoff_to_planner: '交给策划',
                handoff_to_host: '返回助手',
              } as Record<string, string>
            )[part.toolName] ?? part.toolName}{' '}
            · {part.label}
          </span>
        </div>
      )
    case 'artifact':
      return <ArtifactPart part={part} number={number} open={open} />
    case 'plan':
      return (
        <section className="card plan" aria-label="任务计划">
          <h3>
            任务计划
            {part.steps.some((step) => step.status === 'cancelled') && (
              <span className="muted"> · 已取消</span>
            )}
          </h3>
          <p>{part.explanation}</p>
          <ol>
            {part.steps.map((step) => (
              <li key={step.id} className={step.status}>
                <StatusIcon state={step.status} />
                <span>
                  {step.text}
                  {step.status === 'cancelled' && <span className="muted"> · 已取消</span>}
                </span>
              </li>
            ))}
          </ol>
        </section>
      )
    case 'confirm':
      return <ConfirmPart part={part} />
    case 'error':
      return (
        <div role="alert" className="error">
          ! {part.message}
        </div>
      )
    default: {
      const exhaustive: never = part
      return exhaustive
    }
  }
}
