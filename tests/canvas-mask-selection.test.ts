import test from 'node:test'
import assert from 'node:assert/strict'
import { toggleMaskRegion } from '../apps/web/canvas/flows/mask-selection.js'

test('快速选择只切换点击的连通主体，可恢复选区且不修改识别底稿', () => {
  const data = new Uint8ClampedArray(5 * 2 * 4)
  for (const i of [0, 1, 5, 4, 9]) {
    data.fill(255, i * 4, i * 4 + 4)
  }
  const detected = { width: 5, height: 2, data }
  const current = { data: data.slice() }
  assert.equal(toggleMaskRegion(detected, current, 0, 0), true)
  for (const i of [0, 1, 5]) {
    assert.equal(current.data[i * 4], 0)
  }
  for (const i of [4, 9]) {
    assert.equal(current.data[i * 4], 255)
  }
  assert.equal(data[0], 255)
  assert.equal(toggleMaskRegion(detected, current, 0, 1), true)
  assert.equal(current.data[0], 255)
  assert.equal(toggleMaskRegion(detected, current, 2, 0), false)
  assert.equal(toggleMaskRegion(detected, current, 5, 0), false)
})
