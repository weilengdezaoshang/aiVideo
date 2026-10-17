import { factAnchor } from './agent-answer.js'
import type { AgentControlReceipt, AgentEnvelope, AgentNextAction } from './agent/contracts.js'

function displayData(value: unknown): string {
  return JSON.stringify(value, null, 2) ?? '无返回内容'
}

function formatValue(value: unknown): string {
  return Array.isArray(value) ? value.join(' × ') : String(value)
}

function NextActions({ items }: { items: readonly AgentNextAction[] }) {
  if (!items.length) {
    return null
  }
  return (
    <>
      <strong>下一步建议</strong>
      <ul className="next-agent-next">
        {items.map((item, index) => (
          <li key={index} data-priority={item.priority}>
            <span className="next-agent-badge">
              {item.priority === 'required' ? '必需' : '可选'}
            </span>{' '}
            {item.reason}
            {item.tool ? `（${item.tool}）` : ''}
          </li>
        ))}
      </ul>
    </>
  )
}

export function AgentReceipt({ receipt }: { receipt: AgentControlReceipt }) {
  if (receipt.status === 'accepted') {
    return null
  }
  return (
    <div className="next-agent-evidence">
      <p className="next-agent-error">
        {receipt.error?.message}（{receipt.error?.code}）
      </p>
      <NextActions items={receipt.error?.next ?? []} />
    </div>
  )
}

/** Renders a tool result envelope; unparsed (legacy) outputs fall back to the raw JSON. */
export function AgentEvidence({
  envelope,
  raw,
  runId,
}: {
  envelope: AgentEnvelope | null
  raw: unknown
  runId: string
}) {
  if (!envelope) {
    return (
      <>
        <strong>工具返回</strong>
        <pre>{displayData(raw)}</pre>
      </>
    )
  }
  if (envelope.status === 'duplicate') {
    return (
      <div className="next-agent-evidence">
        <p>该调用与 {envelope.dedupeOf} 重复，未再次执行。</p>
        <NextActions items={envelope.next} />
      </div>
    )
  }
  if (envelope.status === 'error') {
    const { error } = envelope
    return (
      <div className="next-agent-evidence">
        <p className="next-agent-error">
          {error.message}（{error.code}，{error.retryable ? '修正后可重试' : '不可重试'}）
        </p>
        {error.details.fields.length > 0 && (
          <ul className="next-agent-facts">
            {error.details.fields.map((field) => (
              <li key={`${field.path}:${field.problem}`}>
                参数 {field.path}：{field.problem}
              </li>
            ))}
          </ul>
        )}
        <NextActions items={error.next} />
      </div>
    )
  }
  const { result } = envelope
  return (
    <div className="next-agent-evidence">
      {!result.coverage.complete && (
        <p className="next-agent-coverage is-partial">
          部分结果：{result.coverage.reason ?? '未覆盖全部对象'}
        </p>
      )}
      <strong>证据</strong>
      <ul className="next-agent-facts">
        {result.evidence.map((fact) => (
          <li key={fact.id} id={factAnchor(runId, fact.id)} data-scope={fact.scope}>
            <code>{fact.id}</code> {fact.statement}：{formatValue(fact.value)}
          </li>
        ))}
      </ul>
      <NextActions items={result.next} />
      <details>
        <summary>原始数据</summary>
        <pre>{displayData(result.data)}</pre>
      </details>
    </div>
  )
}
