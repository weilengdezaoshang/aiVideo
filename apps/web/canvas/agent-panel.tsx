import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import type { ChatMessage } from './state/doc-store.js'
import type { AgentStore } from './agent/store.js'
import { isActiveAgentRun, type AgentRunStatus } from './agent/types.js'
import { AgentMediaActions } from './agent-media-actions.js'
import { AgentAnswer } from './agent-answer.js'
import { AgentActivityLog } from './agent/activity-log.js'
import { AgentPlan } from './agent-plan.js'
import { AgentReflections } from './agent-reflection.js'
import { activityTools } from './agent/activity-log-model.js'

const labels: Record<AgentRunStatus, string> = {
  queued: '等待执行',
  running: '执行中',
  completed: '已完成',
  stopped: '已停止',
  failed: '执行失败',
  needs_attention: '需要处理',
  limit_reached: '已达运行上限',
  waiting_input: '等待你的回答',
  waiting_approval: '等待审批',
  awaiting_confirm: '等待确认',
  awaiting_user_final: '样片已完成',
  canceling: '正在取消',
  cancelled: '已取消',
  blocked: '已拦截',
}
const stageLabels: Record<string, string> = {
  plan: '规划中',
  execute: '执行步骤',
  respond: '组织回答',
  reflect: '复盘中',
  waiting: '等待输入',
  done: '已完成',
}
export function AgentPanel({
  store,
  history,
  getSelection,
  onArtifact,
}: {
  store: AgentStore
  history: readonly ChatMessage[]
  getSelection: () => readonly string[]
  onArtifact?: (nodeId: string) => void
}) {
  const state = useSyncExternalStore(store.subscribe, store.getSnapshot)
  const active = state.runs.find(isActiveAgentRun)
  const listRef = useRef<HTMLDivElement>(null)
  const followRef = useRef(true)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const [stopping, setStopping] = useState(false)
  const stoppingRef = useRef(false)
  const canceling = stopping || active?.status === 'canceling'
  const showStop = Boolean(active) && (!state.hostEnabled || !state.draft.trim() || canceling)
  const stopActive = async () => {
    if (!active || stoppingRef.current || active.status === 'canceling') {
      return
    }
    stoppingRef.current = true
    setStopping(true)
    try {
      await store.stop(active.id)
    } finally {
      stoppingRef.current = false
      setStopping(false)
    }
  }

  const canSend =
    !canceling &&
    !state.loading &&
    !state.sending &&
    (!active || state.hostEnabled) &&
    !state.pendingRequests.length &&
    Boolean(state.draft.trim()) &&
    Boolean(state.modelId)
  useEffect(() => {
    const list = listRef.current
    if (list && followRef.current) {
      list.scrollTop = list.scrollHeight
    }
  }, [state.runs, state.sending])
  return (
    <div className="next-panel-body next-chat-panel next-agent-panel">
      {(state.loading || !state.connected) && (
        <p className="next-agent-connection" role="status">
          {state.loading ? '正在读取对话…' : '正在重新连接…'}
        </p>
      )}
      {history.length > 0 && (
        <details className="next-agent-history">
          <summary>历史对话 · {history.length} 条</summary>
          {history.map((item) => (
            <p key={item.id} className={`is-${item.role}`}>
              {item.text || item.prompt || item.request || item.kind}
            </p>
          ))}
        </details>
      )}
      <div
        className="next-chat-list"
        aria-label="助手运行记录"
        aria-live="polite"
        ref={listRef}
        onScroll={() => {
          const list = listRef.current
          if (list) {
            followRef.current = list.scrollHeight - list.scrollTop - list.clientHeight < 48
          }
        }}
      >
        {!state.loading && !state.runs.length && (
          <div className="next-agent-welcome">
            <span className="next-agent-welcome-mark" aria-hidden="true">
              ✦
            </span>
            <h3>{state.hostEnabled ? '描述你想创作的画面' : '一起梳理你的创意'}</h3>
            <div className="next-agent-suggestions">
              {(state.hostEnabled
                ? ['帮选中的图片抠图', '生成四方向画面', '把选中的图片做成视频']
                : ['画布上有哪些素材？', '检查选中素材的参考关系', '帮我梳理当前分镜']
              ).map((prompt) => (
                <button
                  type="button"
                  key={prompt}
                  onClick={() => {
                    store.setDraft(prompt)
                    inputRef.current?.focus()
                  }}
                >
                  {prompt}
                  <span aria-hidden="true">↗</span>
                </button>
              ))}
            </div>
          </div>
        )}
        {state.runs.map((run) => (
          <article className="next-agent-run" key={run.id}>
            <p className="is-user">{run.message}</p>
            <div className="next-agent-response-card">
              <div className="next-agent-speaker">
                <span className="next-agent-speaker-mark" aria-hidden="true">
                  ✦
                </span>
                {run.persona?.displayName || '帧屿助手'}
              </div>
              {!(run.media && /^\d+\/\d+ 个创作步骤完成。$/.test(run.reply ?? '')) && (
                <AgentAnswer run={run} />
              )}
              <section className="next-agent-process" aria-label="处理过程">
                <div className="next-agent-process-status">
                  {((!activityTools(run).length && !run.modelCalls.length) ||
                    !isActiveAgentRun(run)) && (
                    <span className="next-agent-latest" role="status">
                      {isActiveAgentRun(run) && run.stage
                        ? (stageLabels[run.stage] ?? run.stage)
                        : run.canvasAction && run.status === 'waiting_approval'
                          ? '等待确认抠图'
                          : labels[run.status]}
                    </span>
                  )}
                  <details className="next-agent-info">
                    <summary aria-label="查看执行信息" title="查看执行信息">
                      ⋯
                    </summary>
                    <div className="next-agent-info-content">
                      <div className="next-agent-run-meta">
                        {isActiveAgentRun(run) && run.stage && (
                          <span>{stageLabels[run.stage] ?? run.stage}</span>
                        )}
                        <span>画布版本 {run.baseRevision}</span>
                        <span>
                          {state.models.find((model) => model.id === run.modelId)?.label ||
                            run.modelId}
                        </span>
                      </div>
                      {run.media && (
                        <p className="next-agent-hint">{run.media.chargedCredits} 积分已结算</p>
                      )}
                      <AgentPlan run={run} />
                      <AgentReflections run={run} />
                    </div>
                  </details>
                </div>
                <AgentActivityLog run={run} current={run.id === state.runs.at(-1)?.id} />
              </section>
              {Boolean(run.media?.refundCredits) && (
                <p className="next-agent-hint">{run.media?.refundCredits} 积分已退还</p>
              )}
              {onArtifact && run.media?.children.some((child) => child.status === 'completed') && (
                <div className="next-agent-results">
                  {run.media.children
                    .filter((child) => child.status === 'completed')
                    .map((child) => (
                      <button
                        type="button"
                        className="next-agent-result"
                        key={child.targetNode}
                        onClick={() => onArtifact(child.targetNode)}
                      >
                        <span className="next-agent-result-icon" aria-hidden="true">
                          <svg
                            width="20"
                            height="20"
                            viewBox="0 0 24 24"
                            fill="none"
                            stroke="currentColor"
                            strokeWidth="1.5"
                            strokeLinecap="round"
                            strokeLinejoin="round"
                          >
                            <rect x="3" y="3" width="18" height="18" rx="3" />
                            <circle cx="8" cy="8" r="1" />
                            <path d="m3 16 6-6 5 5 3-3 4 4" />
                          </svg>
                        </span>
                        <span className="next-agent-result-info">
                          <strong>
                            {child.title}
                            {child.index ? ` ${child.index + 1}` : ''}
                          </strong>
                          <span>{run.media?.tier === 'final' ? '正片' : '样片'}</span>
                        </span>
                        <span className="next-agent-result-action">
                          在画布查看 <span aria-hidden="true">↗</span>
                        </span>
                      </button>
                    ))}
                </div>
              )}
              {run.canvasAction && run.status === 'waiting_approval' && (
                <section className="next-agent-action-section" aria-label="抠图确认">
                  <strong>在画布确认主体选区</strong>
                  <p>可用画笔和橡皮擦修正，点击图片旁的「抠图」后生成透明背景图片。</p>
                  <div className="next-agent-action-buttons">
                    <button
                      type="button"
                      className="is-primary"
                      onClick={() =>
                        state.pendingCanvasResults[run.id]
                          ? void store.retryCanvasResult(run.id)
                          : store.openCanvasAction(run.id)
                      }
                    >
                      {state.pendingCanvasResults[run.id] ? '同步抠图结果' : '进入抠图模式'}
                    </button>
                    <button
                      type="button"
                      disabled={state.sending}
                      onClick={() => void store.stop(run.id)}
                    >
                      取消抠图
                    </button>
                  </div>
                </section>
              )}
              {run.canvasAction?.resultObjectId && run.status === 'completed' && onArtifact && (
                <button
                  type="button"
                  className="next-agent-result"
                  onClick={() => onArtifact(run.canvasAction!.resultObjectId!)}
                >
                  在画布查看抠图结果 ↗
                </button>
              )}
              <AgentMediaActions
                run={run}
                store={store}
                sending={state.sending}
                finalRun={state.runs.find((item) => item.media?.previewRunId === run.id)}
              />
              {run.media?.cancelReason === 'user_revised' && (
                <p className="next-agent-hint">
                  {run.status === 'canceling'
                    ? '已请求取消上一批，成功作品保留。'
                    : '已取消上一批，成功作品保留。'}
                </p>
              )}
              {run.waiting && (
                <div className="next-agent-waiting" role="status">
                  <strong>
                    {'question' in run.waiting ? '需要你补充信息' : '操作等待你的审批'}
                  </strong>
                  <p>
                    {'question' in run.waiting
                      ? run.waiting.question
                      : `${run.waiting.tool} ${JSON.stringify(run.waiting.arguments)}`}
                  </p>
                  {'question' in run.waiting &&
                    run.status === 'waiting_input' &&
                    run.waiting.options?.map((option) => (
                      <button
                        key={option.id}
                        type="button"
                        disabled={state.sending}
                        onClick={() => {
                          store.setDraft(option.label)
                          void store.send(getSelection(), { runId: run.id, optionId: option.id })
                        }}
                      >
                        {option.label}
                      </button>
                    ))}
                  {'question' in run.waiting && run.status === 'waiting_input' && (
                    <button
                      type="button"
                      disabled={state.sending || !state.draft.trim()}
                      onClick={() => {
                        void store.send(getSelection(), null)
                      }}
                    >
                      作为新需求发送
                    </button>
                  )}
                  {!('question' in run.waiting) && (
                    <p className="next-agent-hint">暂不支持审批，操作未执行。</p>
                  )}
                </div>
              )}
              {run.batch && run.batch.totalItems > 0 && (
                <p className="next-agent-hint">
                  批量成功 {run.batch.completedItems}/{run.batch.totalItems}
                  ，已处理{' '}
                  {run.batch.settledItems ?? run.batch.processedItems ?? run.batch.completedItems}/
                  {run.batch.totalItems}
                  {run.batch.failedCount ? `，失败 ${run.batch.failedCount}` : ''}
                  {run.batch.skippedCount ? `，跳过 ${run.batch.skippedCount}` : ''}
                </p>
              )}
              {run.batch && run.batch.failedCount > 0 && run.batch.retryableItemIds?.length ? (
                <button
                  type="button"
                  disabled={state.sending}
                  onClick={() =>
                    void store.retryBatchItems(
                      run.parent_run_id || run.id,
                      run.batch?.retryableItemIds || [],
                    )
                  }
                >
                  只重做失败项
                </button>
              ) : null}
              {run.error && !(run.media && run.status === 'failed') && (
                <p className="next-agent-error">{run.error}</p>
              )}
            </div>
          </article>
        ))}
      </div>
      {state.pendingRequests.map((request) => (
        <div className="next-agent-pending" role="status" key={request.clientRequestId}>
          <p>这条消息的提交结果待确认：{request.message}</p>
          <button type="button" disabled={state.sending} onClick={() => void store.refresh()}>
            查询原请求
          </button>
          <button
            type="button"
            disabled={state.sending}
            onClick={() => void store.retryPending(request.clientRequestId)}
          >
            用原请求重试
          </button>
        </div>
      ))}
      {state.error && (
        <p role="alert" className="next-agent-error">
          {state.error}
        </p>
      )}
      <form
        className="next-agent-composer"
        onSubmit={(event) => {
          event.preventDefault()
          if (canSend) {
            followRef.current = true
            void store.send(getSelection())
          }
        }}
      >
        <textarea
          ref={inputRef}
          rows={2}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault()
              if (canSend) {
                followRef.current = true
                void store.send(getSelection())
              }
            }
          }}
          aria-label="向画布助手提问"
          value={state.draft}
          onChange={(event) => store.setDraft(event.target.value)}
          placeholder="描述你的想法…"
        />
        <div className="next-agent-composer-toolbar">
          {!state.hostEnabled && (
            <label className="next-agent-model">
              <span>模型</span>
              <select
                aria-label="助手模型"
                value={state.modelId}
                disabled={state.sending || Boolean(active)}
                onChange={(event) => store.setModel(event.target.value)}
              >
                {!state.models.length && <option value="">暂无可用模型</option>}
                {state.models.map((model) => (
                  <option key={model.id} value={model.id}>
                    {model.label}
                  </option>
                ))}
              </select>
            </label>
          )}
          {active && !showStop && (
            <button
              type="button"
              className="next-agent-composer-stop"
              aria-label="停止"
              title="停止当前生成"
              disabled={canceling}
              onClick={() => void stopActive()}
            >
              <span className="next-agent-stop-square" aria-hidden="true" />
            </button>
          )}
          {showStop ? (
            <button
              type="button"
              className="next-agent-send is-stop"
              aria-label={canceling ? '正在停止' : '停止'}
              title={canceling ? '正在停止' : '停止当前生成'}
              disabled={canceling}
              onClick={() => void stopActive()}
            >
              <span className="next-agent-stop-square" aria-hidden="true" />
            </button>
          ) : (
            <button
              type="submit"
              disabled={!canSend}
              className="next-agent-send"
              aria-label={state.sending ? '提交中…' : active ? '修改需求' : '发送'}
            >
              <svg
                width="18"
                height="18"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
              >
                <path d="m6 12 6-6 6 6M12 6v13" />
              </svg>
            </button>
          )}
        </div>
      </form>
    </div>
  )
}
