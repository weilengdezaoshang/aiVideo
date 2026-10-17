import { useState } from 'react'
import type { AgentRun } from './agent/types.js'
import type { AgentStore } from './agent/store.js'

const failureReasons: Readonly<Record<string, string>> = {
  UPSTREAM_UNAVAILABLE: '暂时无法连接生成服务，请稍后重试。',
  UPSTREAM_TIMEOUT: '生成服务响应超时，请稍后重试。',
  INVALID_PARAM: '生成参数不受当前模型支持，请调整要求后重试。',
  AUTH_FAILED: '模型凭据验证失败，请检查 API 配置。',
  INSUFFICIENT_CREDITS: '可用积分不足，请检查余额后重试。',
}

export function AgentMediaActions({
  run,
  store,
  sending,
  finalRun,
}: {
  run: AgentRun
  store: AgentStore
  sending: boolean
  finalRun?: AgentRun
}) {
  const [confirmFinal, setConfirmFinal] = useState(false)
  const [pending, setPending] = useState<string | null>(null)
  const media = run.media
  if (!media) {
    return null
  }
  const busy = sending || pending !== null
  const perform = async (key: string, action: () => Promise<void>) => {
    setPending(key)
    try {
      await action()
    } finally {
      setPending(null)
    }
  }
  if (run.status === 'awaiting_confirm') {
    return (
      <section className="next-agent-action-section" aria-label="确认创作">
        <strong>{media.tier === 'final' ? '确认生成正片' : '确认本次创作'}</strong>
        <p>预计消耗 {media.pendingQuoteCredits ?? media.quoteCredits} 积分，确认后开始生成。</p>
        <div className="next-agent-action-buttons">
          <button
            type="button"
            className="is-primary"
            disabled={busy}
            onClick={() => void perform('approve', () => store.confirm(run.id, true))}
          >
            {pending === 'approve' ? '正在提交…' : '确认生成'}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => void perform('cancel', () => store.confirm(run.id, false))}
          >
            取消
          </button>
        </div>
      </section>
    )
  }
  if (run.status === 'failed') {
    const failed = media.children.filter((child) => child.status === 'failed')
    return (
      <section className="next-agent-action-section is-failure" aria-label="生成失败">
        <strong>{media.tier === 'final' ? '正片生成未完成' : '本次生成未完成'}</strong>
        {media.completed > 0 && <p>已完成的 {media.completed} 个作品保留在画布中。</p>}
        {failed.map((child) => {
          const key = `${child.stepId}:${child.index}`
          return (
            <div className="next-agent-failed-task" key={key}>
              <div className="next-agent-failed-task-title">
                {child.title}
                {child.index ? ` ${child.index + 1}` : ''}
              </div>
              <p>
                {failureReasons[child.errorCode ?? ''] ||
                  '这一步未能完成，可以重试或调整创作要求。'}
              </p>
              <div className="next-agent-action-buttons">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    void perform(key, () => store.retry(run.id, child.stepId, child.index))
                  }
                >
                  {pending === key ? '正在提交…' : '重试生成'}
                </button>
                <span>{child.retryCredits} 积分起</span>
              </div>
              {child.errorCode && (
                <details className="next-agent-error-code">
                  <summary>错误信息</summary>
                  <code>{child.errorCode}</code>
                </details>
              )}
            </div>
          )
        })}
        {!failed.length && <p>{run.error || '请稍后重试，或调整创作要求。'}</p>}
      </section>
    )
  }
  if (run.status !== 'awaiting_user_final') {
    return null
  }
  if (finalRun) {
    return (
      <p className="next-agent-final-followup" role="status">
        {finalRun.status === 'completed'
          ? '正片已生成，可在下方查看。'
          : finalRun.status === 'failed'
            ? '正片生成失败，可在对应任务中重试。'
            : '正片任务已创建，请查看后续进度。'}
      </p>
    )
  }
  return (
    <section className="next-agent-action-section" aria-label="样片转正片">
      <strong>{confirmFinal ? '生成这组样片的正片？' : '样片已就绪'}</strong>
      <p>
        {media.goalType === 'video'
          ? '正片会按样片方案重新渲染，画面可能变化。'
          : '以这组样片为参考生成正片，样片会保留。'}
      </p>
      {confirmFinal && <p>生成正片会额外消耗积分，具体费用按当前任务计价。</p>}
      <div className="next-agent-action-buttons">
        <button
          type="button"
          className="is-primary"
          disabled={busy}
          onClick={() => {
            if (!confirmFinal) {
              setConfirmFinal(true)
              return
            }
            void perform('final', () => store.final(run.id))
          }}
        >
          {pending === 'final' ? '正在提交…' : confirmFinal ? '确认生成正片' : '生成正片'}
        </button>
        {confirmFinal && (
          <button type="button" disabled={busy} onClick={() => setConfirmFinal(false)}>
            暂不生成
          </button>
        )}
      </div>
    </section>
  )
}
