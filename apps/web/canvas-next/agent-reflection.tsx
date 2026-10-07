import type {
  AgentReflectionRecord,
  AgentReflectionSource,
  AgentRun,
} from '../canvas/agent/types.js'
import type { AgentIssueKind } from '../canvas/agent/contracts.js'

const sourceLabels: Record<AgentReflectionSource, string> = {
  blocker: '受阻复盘',
  hard_check: '硬检验',
  model: '模型复盘',
}
const verdictLabels = {
  accept: '通过',
  revise_answer: '回答需修订',
  retry_step: '步骤需重试',
  replan: '需要重新规划',
  ask_user: '需要用户澄清',
} as const
const issueKindLabels: Record<AgentIssueKind, string> = {
  ungrounded: '缺乏依据',
  incomplete: '不完整',
  truncated: '受截断影响',
  off_target: '偏离目标',
  stale: '依据过期',
}

function HardChecks({ checks }: { checks: NonNullable<AgentReflectionRecord['hardChecks']> }) {
  return (
    <ul className="next-agent-facts">
      {checks.checks.map((check) => (
        <li key={check.id} data-check={check.id} data-passed={check.passed ? 'true' : 'false'}>
          {check.id}：{check.passed ? '通过' : '未通过'}
          {!check.passed &&
            check.issues.map((issue, index) => (
              <span key={index} className="next-agent-issue">
                {' '}
                {issue.detail}
              </span>
            ))}
        </li>
      ))}
    </ul>
  )
}

/** One card per recorded reflection: hard checks item by item plus semantic issues. */
export function AgentReflections({ run }: { run: AgentRun }) {
  const reflections = run.reflections ?? []
  if (!reflections.length) {
    return null
  }
  return (
    <details className="next-agent-reflection">
      <summary>复盘 · {reflections.length} 条</summary>
      {reflections.map((item) => (
        <article className="next-agent-reflection-item" key={item.id} data-source={item.source}>
          <p className="next-agent-reflection-verdict">
            {sourceLabels[item.source]}：{verdictLabels[item.verdict]}
          </p>
          {item.hardChecks && <HardChecks checks={item.hardChecks} />}
          {item.issues.length > 0 && (
            <ul className="next-agent-facts">
              {item.issues.map((issue, index) => (
                <li key={index}>
                  {issueKindLabels[issue.kind]}
                  {issue.stepKey ? `（${issue.stepKey}）` : ''}：{issue.problem}
                </li>
              ))}
            </ul>
          )}
          {item.question && <p className="next-agent-step-summary">待澄清：{item.question}</p>}
        </article>
      ))}
    </details>
  )
}
