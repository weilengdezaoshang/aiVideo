import test from 'node:test'
import assert from 'node:assert/strict'
import { wheelZoomFactor } from '../apps/web/canvas/engine/stage.js'

test('触控板捏合方向：向外放大，向内缩小', () => {
  assert.ok(wheelZoomFactor(-1) > 1, '向外捏合产生负 deltaY，应放大画布')
  assert.ok(wheelZoomFactor(1) < 1, '向内捏合产生正 deltaY，应缩小画布')
})

test('鼠标滚轮与触控板使用相同缩放方向', () => {
  assert.equal(wheelZoomFactor(-100), wheelZoomFactor(-1))
  assert.equal(wheelZoomFactor(100), wheelZoomFactor(1))
})
