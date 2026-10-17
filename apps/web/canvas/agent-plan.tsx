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
      <p className="next-agent-step-heading">
        <span className="next-agent-step-marker" aria-hidden="true">
          {step.status === 'completed'
            ? '✓'
            : step.status === 'running'
              ? '◌'
              : step.status === 'blocked'
                ? '!'
                : '○'}
        </span>
        <span className="next-agent-step-title">{step.title}</span>
        <span className="next-agent-step-state">
          {stepLabels[step.status]}
          {step.retries > 0 ? ` · 重试 ${step.retries} 次` : ''}
        </span>
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
  return (
    <details className="next-agent-plan" open>
      <summary>
        <span>{run.media ? `进程 ${run.media.completed}/${run.media.total}` : '生成计划'}</span>
        <span className="next-agent-plan-count">
          {run.media?.completed ?? live.filter((step) => step.status === 'completed').length} /{' '}
          {run.media?.total ?? live.length} 已完成
        </span>
      </summary>
      <p className="next-agent-plan-goal">{run.plan.goal}</p>
      <progress
        aria-label="计划完成进度"
        max={Math.max(1, run.media?.total ?? live.length)}
        value={run.media?.completed ?? live.filter((step) => step.status === 'completed').length}
      />
      <div className="next-agent-plan-caption">
        任务清单 <span>v{version}</span>
      </div>
      <ol className="next-agent-steps">
        {run.intentTrace?.multi_step
          ? run.intentTrace.steps.map((semantic, index) => {
              const children =
                run.media?.children.filter((child) => child.intentStepId === semantic.id) ?? []
              const members = live.filter((step) =>
                children.some((child) => child.stepId === step.key),
              )
              const state =
                run.intentTrace?.step_results.find((item) => item.id === semantic.id)?.status ??
                'pending'
              return (
                <li key={semantic.id}>
                  <p>
                    第 {index + 1} 步 ·{' '}
                    {semantic.l2 === 'image_to_multiframe'
                      ? '生成视频'
                      : semantic.l2 === 'text_to_image'
                        ? '生成图片'
                        : '编辑图片'}{' '}
                    · {stepLabels[state]}
                  </p>
                  <ol>
                    {members.map((step) => (
                      <StepRow
                        key={step.id}
                        run={run}
                        step={step}
                        current={step.key === run.plan?.current}
                      />
                    ))}
                  </ol>
                </li>
              )
            })
          : live.map((step) => (
              <StepRow
                key={step.id}
                run={run}
                step={step}
                current={step.key === run.plan?.current}
              />
            ))}
      </ol>
    </details>
  )
}
