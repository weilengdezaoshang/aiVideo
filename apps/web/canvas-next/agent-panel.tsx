import { useSyncExternalStore } from 'react'
import type { ChatMessage } from '../canvas/state/doc-store.js'
import type { AgentStore } from '../canvas/agent/store.js'
import { parseControlReceipt, parseEnvelope } from '../canvas/agent/contracts.js'
import { isActiveAgentRun, type AgentRunStatus } from '../canvas/agent/types.js'
import { AgentAnswer, toolAnchor } from './agent-answer.js'
import { AgentEvidence, AgentReceipt } from './agent-evidence.js'

const labels: Record<AgentRunStatus, string> = {
  queued: '等待执行',
  running: '执行中',
  completed: '已完成',
  stopped: '已停止',
  failed: '执行失败',
  needs_attention: '需要处理',
  limit_reached: '已达运行上限',
}
const toolLabels = {
  pending: '等待调用',
  running: '执行中',
  completed: '已返回',
  failed: '失败',
  cancelled: '已取消',
  late: '迟到结果已归档',
}
function displayData(value: unknown): string {
  const text = JSON.stringify(value, null, 2) ?? '无返回内容'
  return text.length > 12000 ? `${text.slice(0, 12000)}\n…显示已截断` : text
}

function ReceiptView({ output }: { output: unknown }) {
  const receipt = parseControlReceipt(output)
  return receipt ? <AgentReceipt receipt={receipt} /> : <pre>{displayData(output)}</pre>
}

export function AgentPanel({
  store,
  history,
  getSelection,
}: {
  store: AgentStore
  history: readonly ChatMessage[]
  getSelection: () => readonly string[]
}) {
  const state = useSyncExternalStore(store.subscribe, store.getSnapshot)
  const active = state.runs.find(isActiveAgentRun)
  return (
    <div className="next-panel-body next-chat-panel next-agent-panel">
      <div className="next-agent-header">
        <strong>画布助手</strong>
        <span>{state.loading ? '正在读取' : state.connected ? '已连接' : '正在重连'}</span>
      </div>
      <p className="next-agent-hint">
        读取已保存的画布并调用检查工具。当前不会生成图片或修改画布。
      </p>
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
      <div className="next-chat-list" aria-label="助手运行记录" aria-live="polite">
        {!state.loading && !state.runs.length && <p>描述你的问题，例如“画布上有哪些图片？”</p>}
        {state.runs.map((run) => (
          <article className="next-agent-run" key={run.id}>
            <p className="is-user">{run.message}</p>
            <div className="next-agent-run-meta">
              <span>{labels[run.status]}</span>
              <span>画布版本 {run.baseRevision}</span>
              <span>
                {state.models.find((model) => model.id === run.modelId)?.label || run.modelId}
              </span>
            </div>
            {run.toolCalls.map((call) => (
              <details
                className="next-agent-tool"
                key={call.id}
                id={call.evidenceId ? toolAnchor(run.id, call.evidenceId) : undefined}
              >
                <summary>
                  {call.evidenceId ? `${call.evidenceId} · ` : ''}
                  {call.name} · {toolLabels[call.status]}
                </summary>
                <strong>输入</strong>
                <pre>{displayData(call.arguments)}</pre>
                {call.output !== undefined && call.kind === 'control' ? (
                  <ReceiptView output={call.output} />
                ) : (
                  call.output !== undefined && (
                    <AgentEvidence
                      envelope={parseEnvelope(call.output)}
                      raw={call.output}
                      runId={run.id}
                    />
                  )
                )}
                {call.error && <p className="next-agent-error">{call.error}</p>}
              </details>
            ))}
            <AgentAnswer run={run} />
            {run.error && <p className="next-agent-error">{run.error}</p>}
            <div className="next-agent-run-meta">
              <span>模型调用 {run.modelCallCount ?? run.modelCalls.length} 次</span>
              <span>工具调用 {run.toolCallCount ?? run.toolCalls.length} 次</span>
              {isActiveAgentRun(run) && (
                <button type="button" onClick={() => void store.stop(run.id)}>
                  停止
                </button>
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
        onSubmit={(event) => {
          event.preventDefault()
          void store.send(getSelection())
        }}
      >
        <label>
          模型
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
        <textarea
          aria-label="向画布助手提问"
          value={state.draft}
          onChange={(event) => store.setDraft(event.target.value)}
          placeholder="输入你的问题…"
        />
        <button
          type="submit"
          disabled={
            state.loading ||
            state.sending ||
            Boolean(active) ||
            Boolean(state.pendingRequests.length) ||
            !state.draft.trim() ||
            !state.modelId
          }
        >
          {state.sending ? '正在提交…' : active ? '助手正在执行' : '发送'}
        </button>
      </form>
    </div>
  )
}
