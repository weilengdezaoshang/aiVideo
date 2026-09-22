import test from 'node:test'
import assert from 'node:assert/strict'
import { nextMenuIndex } from '../apps/web/shared/menu-navigation.js'

test('公共菜单方向键循环、跳过禁用项，Home/End 定位可用边界', () => {
  const disabled = [true, false, true, false, true]
  assert.equal(nextMenuIndex(disabled, -1, 'ArrowDown'), 1)
  assert.equal(nextMenuIndex(disabled, -1, 'ArrowUp'), 3)
  assert.equal(nextMenuIndex(disabled, 1, 'ArrowDown'), 3)
  assert.equal(nextMenuIndex(disabled, 3, 'ArrowDown'), 1)
  assert.equal(nextMenuIndex(disabled, 1, 'ArrowUp'), 3)
  assert.equal(nextMenuIndex(disabled, 3, 'Home'), 1)
  assert.equal(nextMenuIndex(disabled, 1, 'End'), 3)
  assert.equal(nextMenuIndex([true], 0, 'ArrowDown'), -1)
  assert.equal(nextMenuIndex([], -1, 'Home'), -1)
})
