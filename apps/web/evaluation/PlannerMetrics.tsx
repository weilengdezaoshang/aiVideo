import type { PlannerEvidence } from './api.js'

function rate(hits: number, denominator: number, value: number | null): string {
  if (denominator === 0 || value === null) {
    return `未测（0/${denominator}）`
  }
  return `${(value * 100).toFixed(1)}%（${hits}/${denominator}）`
}

export function PlannerMetrics({ planner }: { planner: PlannerEvidence }) {
  const tokenMean = planner.tokens.mean === null ? 'N/A' : planner.tokens.mean.toFixed(2)
  return (
    <section className="eval-planner-metrics" aria-label="Planner 指标">
      <h4>Planner 指标</h4>
      <p>
        {planner.llmTested
          ? '本次标记为已接入 LLM factory。'
          : `LLM 未测。${planner.llmUntestedReason ?? '没有模型质量结论。'}`}
        {planner.synthetic ? ' 结果为 synthetic，只验证协议，不进入真实质量排行。' : ''}
      </p>
      <dl>
        <div>
          <dt>工具</dt>
          <dd>{rate(planner.tools.hits, planner.tools.denominator, planner.tools.rate)}</dd>
        </div>
        <div>
          <dt>顺序</dt>
          <dd>{rate(planner.order.hits, planner.order.denominator, planner.order.rate)}</dd>
        </div>
        <div>
          <dt>约束</dt>
          <dd>
            {rate(planner.invariants.hits, planner.invariants.denominator, planner.invariants.rate)}
          </dd>
        </div>
        <div>
          <dt>首次审核通过</dt>
          <dd>
            {rate(
              planner.firstReviewPass.hits,
              planner.firstReviewPass.denominator,
              planner.firstReviewPass.rate,
            )}
          </dd>
        </div>
        <div>
          <dt>延迟 P50 / P95</dt>
          <dd>
            {planner.latencyMs.p50 ?? 'N/A'} / {planner.latencyMs.p95 ?? 'N/A'} ms（n=
            {planner.latencyMs.n}）
          </dd>
        </div>
        <div>
          <dt>token / 例</dt>
          <dd>
            {tokenMean}（{planner.tokens.denominator} 例有 token）
          </dd>
        </div>
        <div>
          <dt>覆盖率</dt>
          <dd>
            {rate(planner.coverage.scored, planner.coverage.denominator, planner.coverage.rate)}
          </dd>
        </div>
      </dl>
      <p>
        工具目录 {planner.toolCatalogVersion ?? '未知'} · 提示词哈希{' '}
        <code>{planner.promptTemplateHash ?? '无'}</code>
      </p>
    </section>
  )
}
