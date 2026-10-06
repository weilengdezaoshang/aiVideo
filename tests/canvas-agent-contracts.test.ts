import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { CONTRACT_PARSERS } from '../apps/web/canvas/agent/contracts.js'

type Case = { name: string; model: keyof typeof CONTRACT_PARSERS; valid: boolean; value: unknown }
const cases = JSON.parse(
  readFileSync(new URL('./fixtures/agent-contracts/cases.json', import.meta.url), 'utf8'),
) as Case[]

for (const item of cases) {
  test(`agent contract: ${item.name}`, () => {
    const parsed = CONTRACT_PARSERS[item.model](item.value)
    if (item.valid) {
      assert.deepEqual(parsed, item.value)
    } else {
      assert.equal(parsed, null)
    }
  })
}

test('agent contract fixtures cover every family with positive and negative cases', () => {
  for (const model of Object.keys(CONTRACT_PARSERS)) {
    const verdicts = new Set(cases.filter((item) => item.model === model).map((item) => item.valid))
    assert.deepEqual([...verdicts].sort(), [false, true], model)
  }
})
