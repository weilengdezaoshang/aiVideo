import './dom.js'
import test, { afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { cleanup, render, screen } from '@testing-library/react'
import { PlannerMetrics } from '../apps/web/evaluation/PlannerMetrics.js'
import { label } from '../apps/web/evaluation/caseCopy.js'
import type { PlannerEvidence } from '../apps/web/evaluation/api.js'

afterEach(cleanup)

const planner: PlannerEvidence = {
  llmTested: false,
  llmUntestedReason: '未调用 LLM',
  plannerMode: 'rule',
  synthetic: false,
  promptTemplateHash: 'abc123',
  promptTemplateModule: 'backend.core.supervisor.prompts',
  toolCatalogVersion: 'planner-eval-mock',
  toolCatalogHash: 'hash',
  datasetContentHash: 'dataset',
  coverage: { scored: 40, denominator: 96, rate: 40 / 96 },
  tools: { hits: 10, denominator: 96, rate: 10 / 96 },
  order: { hits: 2, denominator: 8, rate: 0.25 },
  invariants: { hits: 0, denominator: 96, rate: 0 },
  firstReviewPass: { hits: 1, denominator: 4, rate: 0.25 },
  latencyMs: { n: 96, p50: 3, p95: 9 },
  tokens: { perCase: [0], total: 0, mean: 0, denominator: 96 },
}

test('planner 任务类型有独立文案', () => {
  assert.equal(label('planner'), 'Planner 计划')
  assert.notEqual(label('planner'), label('agent_flow'))
})

test('空分母显示未测，synthetic 不进入质量排行', () => {
  render(
    <PlannerMetrics
      planner={{
        ...planner,
        llmTested: false,
        plannerMode: 'rule',
        synthetic: true,
        order: { hits: 0, denominator: 0, rate: null },
      }}
    />,
  )
  const section = screen.getByRole('region', { name: 'Planner 指标' })
  assert.match(section.textContent ?? '', /LLM 未测/)
  assert.match(section.textContent ?? '', /未测（0\/0）/)
  assert.match(section.textContent ?? '', /不进入真实质量排行/)
  assert.doesNotMatch(section.textContent ?? '', /本次标记为已接入 LLM/)
})

test('planner 报表显示分母并标明 LLM 未测', () => {
  render(<PlannerMetrics planner={planner} />)
  const section = screen.getByRole('region', { name: 'Planner 指标' })
  assert.match(section.textContent ?? '', /LLM 未测/)
  assert.match(section.textContent ?? '', /10\/96/)
  assert.match(section.textContent ?? '', /2\/8/)
  assert.match(section.textContent ?? '', /1\/4/)
  assert.match(section.textContent ?? '', /40\/96/)
  assert.match(section.textContent ?? '', /abc123/)
  assert.match(section.textContent ?? '', /P50/)
})
