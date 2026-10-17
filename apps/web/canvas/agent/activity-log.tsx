import { useEffect, useId, useRef, useState, useSyncExternalStore, type ReactNode } from 'react'
import { toolAnchor } from '../agent-answer.js'
import { AgentEvidence, AgentReceipt } from '../agent-evidence.js'
import { parseControlReceipt, parseEnvelope } from './contracts.js'
import type { AgentModelCall, AgentRun, AgentToolCall } from './types.js'
import {
  activityTools,
  groupSummary,
  groupStatus,
  groupTools,
  modelTokenText,
  stepText,
  toolCategories,
  toolCategory,
  toolStatusLabels,
  toolSummary,
  type ToolGroup,
} from './activity-log-model.js'

const expandedTools = new Map<string, boolean>()
const userInteracted = new Set<string>()
const listeners = new Map<string, Set<() => void>>()
function setExpanded(id: string, expanded: boolean, manual = false): void {
  if (manual) {
    userInteracted.add(id)
  }
  if (expandedTools.get(id) === expanded) {
    return
  }
  expandedTools.set(id, expanded)
  listeners.get(id)?.forEach((notify) => notify())
}
function useExpansion(id: string, running: boolean, current: boolean) {
  const expanded = useSyncExternalStore(
    (notify) => {
      const entries = listeners.get(id) ?? new Set<() => void>()
      entries.add(notify)
      listeners.set(id, entries)
      return () => {
        entries.delete(notify)
        if (!entries.size) {
          listeners.delete(id)
        }
      }
    },
    () => expandedTools.get(id) ?? false,
  )
  useEffect(() => {
    if (userInteracted.has(id)) {
      return
    }
    if (!current) {
      setExpanded(id, false)
      return
    }
    // Running tools stay compact; only a user action or evidence jump reveals details.
    if (running) {
      return
    }
    const timer = setTimeout(() => {
      if (!userInteracted.has(id)) {
        setExpanded(id, false)
      }
    }, 300)
    return () => clearTimeout(timer)
  }, [id, running, current])
  return {
    expanded,
    toggle: () => setExpanded(id, !expanded, true),
    reveal: () => setExpanded(id, true, true),
  }
}
function Disclosure({
  id,
  anchor,
  running,
  current,
  nested,
  label,
  children,
}: {
  id: string
  anchor?: string
  running: boolean
  current: boolean
  nested?: boolean
  label: ReactNode
  children: ReactNode
}) {
  const { expanded, toggle, reveal } = useExpansion(id, running, current)
  const detailId = useId()
  const root = useRef<HTMLDivElement>(null)
  // Citation targets remain mounted while hidden so fact references can open all ancestors.
  useEffect(() => {
    const element = root.current
    const open = () => reveal()
    element?.addEventListener('agent-reveal', open)
    return () => element?.removeEventListener('agent-reveal', open)
  }, [reveal])
  return (
    <div
      ref={root}
      id={anchor}
      className={`next-agent-activity${nested ? ' is-nested' : ''}`}
      data-running={running || undefined}
    >
      <button
        type="button"
        className="next-agent-activity-row"
        aria-expanded={expanded}
        aria-controls={detailId}
        onClick={toggle}
      >
        {label}
        <span
          className={`next-agent-activity-chevron${expanded ? ' is-open' : ''}`}
          aria-hidden="true"
        >
          ›
        </span>
      </button>
      <div id={detailId} className="next-agent-activity-detail" hidden={!expanded}>
        {children}
      </div>
    </div>
  )
}
function RawData({ value, label }: { value: unknown; label: string }) {
  const [open, setOpen] = useState(false)
  const id = useId()
  return (
    <div className="next-agent-activity-raw">
      <button
        type="button"
        className="next-agent-activity-parameter"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen(!open)}
      >
        {open ? '收起' : '展开'}
        {label}
      </button>
      <pre id={id} hidden={!open}>
        {JSON.stringify(value, null, 2) ?? '无返回内容'}
      </pre>
    </div>
  )
}
function ReceiptView({ output }: { output: unknown }) {
  const receipt = parseControlReceipt(output)
  return receipt ? <AgentReceipt receipt={receipt} /> : <RawData label="返回数据" value={output} />
}
function ToolRow({
  call,
  runId,
  current,
  nested = false,
}: {
  call: AgentToolCall
  runId: string
  current: boolean
  nested?: boolean
}) {
  const category = toolCategories[toolCategory(call)]
  const summary = toolSummary(call)
  const step = stepText(call.stepKey)
  const running = call.status === 'running'
  const envelope = call.output !== undefined ? parseEnvelope(call.output) : null
  return (
    <Disclosure
      id={call.id}
      anchor={call.evidenceId ? toolAnchor(runId, call.evidenceId) : undefined}
      running={running}
      current={current}
      nested={nested}
      label={
        <>
          {!nested && (
            <span className="next-agent-activity-icon" aria-hidden="true">
              {category.icon}
            </span>
          )}
          <span className={`next-agent-activity-category${running ? ' next-agent-shimmer' : ''}`}>
            {category.label}
          </span>
          <span className="next-agent-activity-main" title={summary}>
            {summary}
          </span>
          {step && (
            <span className="next-agent-activity-secondary" title={step}>
              · {step}
            </span>
          )}
          <span
            className={`next-agent-activity-status is-${call.status}`}
            title={
              call.status === 'failed' && call.error ? call.error : toolStatusLabels[call.status]
            }
          >
            {toolStatusLabels[call.status]}
          </span>
        </>
      }
    >
      <div className="next-agent-activity-meta">
        <code>{call.name}</code>
        {call.evidenceId && <span>证据 {call.evidenceId}</span>}
        {call.dedupeOf && <span>重复调用 {call.dedupeOf}</span>}
      </div>
      <dl className="next-agent-tool-fields">
        {Object.entries(call.arguments)
          .filter(
            ([key, value]) =>
              ['title', 'prompt', 'model', 'width', 'height'].includes(key) &&
              (typeof value === 'string' || typeof value === 'number'),
          )
          .map(([key, value]) => (
            <div key={key}>
              <dt>
                {
                  { title: '任务', prompt: '提示词', model: '模型', width: '宽度', height: '高度' }[
                    key
                  ]
                }
              </dt>
              <dd>{String(value)}</dd>
            </div>
          ))}
      </dl>
      <RawData label="参数" value={call.arguments} />
      {call.output !== undefined &&
        (call.kind === 'control' ? (
          <ReceiptView output={call.output} />
        ) : envelope ? (
          <AgentEvidence envelope={envelope} raw={call.output} runId={runId} />
        ) : (
          <RawData label="返回数据" value={call.output} />
        ))}
      {call.error && <p className="next-agent-error">{call.error}</p>}
    </Disclosure>
  )
}
function GroupRow({
  group,
  runId,
  current,
}: {
  group: ToolGroup
  runId: string
  current: boolean
}) {
  const groupId = `${runId}:${group.id}`
  const hasManualChild = group.calls.some(
    (call) => userInteracted.has(call.id) && expandedTools.get(call.id),
  )
  useEffect(() => {
    // Crossing the grouping threshold must not hide a manually expanded child.
    if (hasManualChild && !userInteracted.has(groupId)) {
      setExpanded(groupId, true, true)
    }
  }, [groupId, hasManualChild])
  const category = toolCategories[group.category]
  const running = group.calls.some((call) => call.status === 'running')
  const status = groupStatus(group)
  const statusTitle =
    status === 'failed'
      ? group.calls
          .filter((call) => call.status === 'failed' && call.error)
          .map((call) => call.error)
          .join('；') || toolStatusLabels.failed
      : toolStatusLabels[status]
  return (
    <Disclosure
      id={groupId}
      running={running}
      current={current}
      label={
        <>
          <span className="next-agent-activity-icon" aria-hidden="true">
            {category.icon}
          </span>
          <span className={`next-agent-activity-category${running ? ' next-agent-shimmer' : ''}`}>
            {category.label}
          </span>
          <span className="next-agent-activity-main">· {groupSummary(group)}</span>
          <span className={`next-agent-activity-status is-${status}`} title={statusTitle}>
            {toolStatusLabels[status]}
          </span>
        </>
      }
    >
      <div className="next-agent-activity-group" onClick={() => setExpanded(groupId, true, true)}>
        {group.calls.map((call) => (
          <ToolRow key={call.id} call={call} runId={runId} current={current} nested />
        ))}
      </div>
    </Disclosure>
  )
}
export function AgentThinking({ call, text }: { call: AgentModelCall; text?: string }) {
  const running = call.status === 'running'
  const tokens = modelTokenText(call.usage)
  const status =
    call.error || call.status === 'failed'
      ? '失败'
      : call.status === 'completed'
        ? '已完成'
        : call.status === 'cancelled' || call.status === 'stopped'
          ? '已停止'
          : call.status === 'pending'
            ? '等待中'
            : call.status
  const latest = text
    ?.trim()
    .split('\n')
    .filter((line) => line.trim())
    .at(-1)
  return (
    <div className="next-agent-thinking-block">
      <div className="next-agent-thinking-row">
        <span className={`next-agent-activity-category${running ? ' next-agent-shimmer' : ''}`}>
          {running ? '正在思考' : `思考 ${call.ordinal} · ${status}`}
        </span>
        {running && latest && (
          <span className="next-agent-thinking-preview" title={text}>
            {latest}
          </span>
        )}
        {tokens && <span className="next-agent-activity-secondary">· {tokens}</span>}
      </div>
      {call.error && <p className="next-agent-error">{call.error}</p>}
    </div>
  )
}
export function AgentActivityLog({ run, current }: { run: AgentRun; current: boolean }) {
  return (
    <div className="next-agent-activity-log">
      {run.modelCalls.map((call) => (
        <AgentThinking key={call.id} call={call} />
      ))}
      {groupTools(activityTools(run)).flatMap((group) =>
        group.calls.length >= 3
          ? [<GroupRow key={group.id} group={group} runId={run.id} current={current} />]
          : group.calls.map((call) => (
              <ToolRow key={call.id} call={call} runId={run.id} current={current} />
            )),
      )}
    </div>
  )
}
