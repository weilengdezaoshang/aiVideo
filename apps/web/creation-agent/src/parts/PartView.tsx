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
    <section
      className={`card confirmation ${part.state}`}
      aria-label={part.interrupt_kind === 'clarify' ? '需求确认' : '积分确认'}
    >
      <div className="card-heading">
        <span>✦ {part.interrupt_kind === 'clarify' ? '需求确认' : '积分确认'}</span>
        {part.interrupt_kind !== 'clarify' && (
          <span className="credit">{part.estimateCredits} 积分</span>
        )}
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
      {part.tier && (
        <span className="artifact-tier">{part.tier === 'preview' ? '样片预览' : '正片'}</span>
      )}
      {failed || part.status === 'failed' ? (
        <div className="image-error">{part.qualityIssue ?? '作品加载失败'}</div>
      ) : part.type === 'video' ? (
        <div className="artifact-video">
          <video
            src={part.url}
            controls
            playsInline
            preload="metadata"
            onError={() => setFailed(true)}
          />
          <button
            aria-label={`放大作品 ${number}`}
            onClick={(event) => open(part, event.currentTarget)}
          >
            ↗
          </button>
        </div>
      ) : (
        <button
          className="image-button"
          aria-label={`放大作品 ${number}`}
          onClick={(event) => open(part, event.currentTarget)}
        >
          <img src={part.url} alt={`创作作品 ${number}`} onError={() => setFailed(true)} />
        </button>
      )}
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
    case 'thinking':
      return (
        <details className="tool">
          <summary>{part.summary ?? '思考过程'}</summary>
          <div>{part.text}</div>
        </details>
      )
    case 'tool_call':
    case 'tool_result':
      return (
        <details className="tool">
          <summary>{part.summary}</summary>
          <pre>{part.detail}</pre>
        </details>
      )
    case 'tool_status':
      return (
        <details className={`tool ${part.state}`}>
          <summary>
            <StatusIcon state={part.state} />
            <span>
              {(
                {
                  creative_agent_mcp_gen_text2image_v3: '生图',
                  image_generate: '生图',
                  video_generate: '生成视频',
                  frames_to_video: '合成视频',
                  image_to_video: '图生视频',
                  subject_cutout: '提取主体',
                  handoff_to_planner: '交给策划',
                  handoff_to_host: '返回助手',
                } as Record<string, string>
              )[part.toolName] ?? '创作工具'}{' '}
              · {part.label}
            </span>
          </summary>
          <div>{part.label}</div>
        </details>
      )
    case 'artifact':
      return <ArtifactPart part={part} number={number} open={open} />
    case 'plan':
      return (
        <section className="card plan" aria-label="任务计划">
          <h3>
            进程 {part.steps.filter((step) => step.status === 'completed').length}/
            {part.steps.length}
            {part.steps.some((step) => step.status === 'cancelled') && (
              <span className="muted"> · 已取消</span>
            )}
          </h3>
          <progress
            aria-label="创作进度"
            max={part.steps.length || 1}
            value={part.steps.filter((step) => step.status === 'completed').length}
          />
          {part.steps.filter((step) => step.status === 'in_progress').length > 1 && (
            <p className="muted">
              正在并行执行 {part.steps.filter((step) => step.status === 'in_progress').length}{' '}
              个步骤
            </p>
          )}
          <p>{part.explanation}</p>
          <ol>
            {part.steps.map((step) => (
              <li key={step.id} className={step.status}>
                <StatusIcon state={step.status} />
                <span>
                  {step.title ?? step.text}
                  {step.detail && <small>{step.detail}</small>}
                  {step.status === 'cancelled' && <span className="muted"> · 已取消</span>}
                  {step.status === 'skipped' && <span className="muted"> · 依赖失败，未执行</span>}
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
