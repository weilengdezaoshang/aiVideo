import './dom.js'
import test, { afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import {
  ReviewSamplingSettings,
  defaultReviewSampling,
  reviewReason,
} from '../apps/web/evaluation/ReviewSamplingSettings.js'
import type { ReviewSamplingConfig } from '../apps/web/evaluation/api.js'
import { useState } from 'react'
afterEach(cleanup)
test('默认仅画质风险与5%通过抽检，可关闭随机并调整灰区', () => {
  let selected: ReviewSamplingConfig = defaultReviewSampling
  function Form() {
    const [value, setValue] = useState(defaultReviewSampling)
    return (
      <ReviewSamplingSettings
        value={value}
        onChange={(next) => {
          selected = next
          setValue(next)
        }}
      />
    )
  }
  render(<Form />)
  assert.equal(
    (screen.getByLabelText('Agent 流程失败归因抽检（不改写规则分）') as HTMLInputElement).checked,
    false,
  )
  assert.equal(
    (screen.getByLabelText('包含 mock/stub 管线自测（默认排除）') as HTMLInputElement).checked,
    false,
  )
  fireEvent.click(screen.getByLabelText('随机抽检画质通过题'))
  assert.equal(selected.randomRate, 0)
  fireEvent.change(screen.getByLabelText('灰区上限'), { target: { value: '0.3' } })
  assert.ok(screen.getByRole('alert'))
  fireEvent.click(screen.getByLabelText('启用本次运行自动送审'))
  assert.equal(selected.enabled, false)
  assert.equal(
    (screen.getByLabelText('灰区下限') as HTMLInputElement).closest('fieldset')?.disabled,
    true,
  )
})
test('所有送审原因有中文且未知原码保留', () => {
  for (const code of [
    'judge_error',
    'judge_unreliable',
    'flagged_case',
    'score_gate_conflict',
    'score_ambiguous',
    'score_gray',
    'judge_disagreement',
    'random',
    'agent_flow_failure',
  ]) {
    assert.doesNotMatch(reviewReason(code), /未知/)
  }
  assert.equal(reviewReason('new_rule'), '未知送审原因：new_rule')
})
