import type { AgentRun, AgentStep, AgentStepStatus } from './agent/types.js'
import { factAnchor, jumpTo, toolAnchor } from './agent-answer.js'
import { evidenceOf } from './agent/citations.js'

const stepLabels: Record<AgentStepStatus, string> = {
  pending: '待执行',
  running: '执行中',
  completed: '已完成',
  blocked: '受阻',
  obsolete: '已废弃',
}

function EvidenceChip({ runId, refs }: { runId: string; refs: readonly string[] }) {
  if (!refs.length) {
    return null
  }
  return (
    <span className="next-agent-step-refs">
      {refs.map((ref) => {
        const anchor = ref.includes('.')
          ? factAnchor(runId, ref)
          : toolAnchor(runId, evidenceOf(ref))
        return (
          <a
            key={ref}
            className="next-agent-cite"
            href={`#${anchor}`}
            onClick={(event) => {
              event.preventDefault()
              jumpTo(anchor)
            }}
          >
            {ref}
          </a>
        )
      })}
    </span>
  )
}

function StepRow({ run, step, current }: { run: AgentRun; step: AgentStep; current: boolean }) {
  return (
    <li className={`next-agent-step is-${step.status}`} data-current={current ? 'true' : undefined}>
      <p>
        {step.title} · {stepLabels[step.status]}
        {step.retries > 0 ? ` · 重试 ${step.retries} 次` : ''}
      </p>
      {step.summary && <p className="next-agent-step-summary">{step.summary}</p>}
      <EvidenceChip runId={run.id} refs={step.evidenceRefs} />
    </li>
  )
}

/** Plan checklist of the newest plan version; older revisions stay as obsolete steps. */
export function AgentPlan({ run }: { run: AgentRun }) {
  const steps = run.steps ?? []
  if (!run.plan || !steps.length) {
    return null
  }
  const version = Math.max(...steps.map((step) => step.planVersion))
  const live = steps.filter((step) => step.planVersion === version)
  const hasProgress = live.some((step) => step.status !== 'pending')
  return (
    <details className="next-agent-plan" open={!hasProgress || run.plan.current === live[0]?.key}>
      <summary>
        执行计划 v{version} · {run.plan.goal}
      </summary>
      <ol className="next-agent-steps">
        {live.map((step) => (
          <StepRow key={step.id} run={run} step={step} current={step.key === run.plan?.current} />
        ))}
      </ol>
    </details>
  )
}
