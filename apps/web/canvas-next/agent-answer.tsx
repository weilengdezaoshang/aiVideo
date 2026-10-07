import { evidenceOf, splitCitations } from '../canvas/agent/citations.js'
import { parseEnvelope } from '../canvas/agent/contracts.js'
import type { AgentRun } from '../canvas/agent/types.js'

export const toolAnchor = (runId: string, evidenceId: string): string =>
  `agent-tool-${runId}-${evidenceId}`
export const factAnchor = (runId: string, ref: string): string => `agent-fact-${runId}-${ref}`

/** Evidence and fact ids this Run really produced; a citation outside it is unverified. */
function knownRefs(run: AgentRun): Set<string> {
  const known = new Set<string>()
  for (const call of run.toolCalls) {
    if (call.kind === 'control' || !call.evidenceId) {
      continue
    }
    known.add(call.evidenceId)
    const envelope = parseEnvelope(call.output)
    if (envelope?.status === 'ok') {
      envelope.result.evidence.forEach((fact) => known.add(fact.id))
    }
  }
  return known
}

/** Scrolls a citation target into view, opening every collapsed ancestor on the way. */
export function jumpTo(id: string): void {
  const target = document.getElementById(id)
  if (!target) {
    return
  }
  for (let node = target.parentElement; node; node = node.parentElement) {
    if (node.tagName === 'DETAILS') {
      ;(node as HTMLDetailsElement).open = true
    }
  }
  target.scrollIntoView?.({ block: 'nearest' })
}

export function AgentAnswer({ run }: { run: AgentRun }) {
  const text = run.reply
  if (!text) {
    return null
  }
  const known = knownRefs(run)
  const verification = run.claims?.verification
  const failed = verification?.checks.filter((check) => !check.passed) ?? []
  return (
    <div className="next-agent-answer">
      <p className="is-assistant">
        {splitCitations(text).map((part, index) => {
          if (part.kind === 'text') {
            return <span key={index}>{part.text}</span>
          }
          if (!known.has(part.ref)) {
            return (
              <span
                key={index}
                className="next-agent-cite is-unknown"
                title="本次运行中不存在该证据"
              >
                {part.ref}
              </span>
            )
          }
          const anchor = part.ref.includes('.')
            ? factAnchor(run.id, part.ref)
            : toolAnchor(run.id, evidenceOf(part.ref))
          return (
            <a
              key={index}
              className="next-agent-cite"
              href={`#${anchor}`}
              onClick={(event) => {
                event.preventDefault()
                jumpTo(anchor)
              }}
            >
              {part.ref}
            </a>
          )
        })}
      </p>
      {verification && (
        <p
          className={
            verification.passed ? 'next-agent-verdict is-pass' : 'next-agent-verdict is-fail'
          }
        >
          {verification.passed ? '已通过证据检验' : '未通过证据检验，以下回答仅为草稿'}
        </p>
      )}
      {failed.length > 0 && (
        <ul className="next-agent-facts">
          {failed.flatMap((check) =>
            check.issues.map((issue, index) => (
              <li key={`${check.id}:${index}`}>
                {check.id}：{issue.detail}
              </li>
            )),
          )}
        </ul>
      )}
    </div>
  )
}
