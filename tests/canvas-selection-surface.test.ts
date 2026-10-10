import test from 'node:test'
import assert from 'node:assert/strict'
import { selectionSurface, selectionAnchor } from '../apps/web/canvas/selection-surface.js'
import type { CanvasObj } from '../apps/web/canvas/state/commands.js'
const image: CanvasObj = { id: 'a', kind: 'image', x: 100, y: 100, width: 200, height: 100 }
test('空节点、成品、时间线和多选使用不同编辑界面，完成产物保留generationRef不变成执行中', () => {
  assert.equal(selectionSurface([]), 'none')
  assert.equal(selectionSurface([image, image]), 'multi')
  assert.equal(selectionSurface([{ ...image, generationRef: { operationId: 'done' } }]), 'image')
  assert.equal(
    selectionSurface([{ ...image, kind: 'placeholder', mediaType: 'video' }]),
    'video-empty',
  )
  assert.equal(
    selectionSurface([{ ...image, kind: 'placeholder', placeholderType: 'timeline' }]),
    'timeline',
  )
  assert.equal(
    selectionSurface([
      { ...image, kind: 'placeholder', generationRef: { operationId: 'running' } },
    ]),
    'busy',
  )
})
test('浮层随相机和旋转边界投影，位置保持在视口中', () => {
  const viewport = { width: 1000, height: 800 }
  assert.deepEqual(selectionAnchor([image], { x: 50, y: 50, scale: 2 }, viewport), {
    x: 300,
    top: 318,
    bottom: 376,
  })
  const rotated = selectionAnchor([{ ...image, rotation: 90 }], { x: 0, y: 0, scale: 1 }, viewport)
  assert.ok(Math.abs(rotated.x - 50) < 0.00001)
  assert.equal(rotated.bottom, 376)
  const outside = selectionAnchor(
    [{ ...image, x: -1000, y: 3000 }],
    { x: 0, y: 0, scale: 1 },
    viewport,
  )
  assert.equal(outside.x, 24)
  assert.equal(outside.bottom, 580)
})
