import test from 'node:test'
import assert from 'node:assert/strict'
import { applyCommand, cmdUpdateObject, invertCommand } from '../apps/web/canvas/state/commands.js'
import {
  resizeFromCorner,
  resizeCursor,
  rotateAroundCenter,
  transformPoint,
  transformBounds,
} from '../apps/web/canvas/engine/transform-geometry.js'

const close = (a: number, b: number) => assert.ok(Math.abs(a - b) < 1e-8, `${a} ≠ ${b}`)

test('四角缩放固定对角并保持图片比例,包括旋转后的图片', () => {
  for (const rotation of [0, 35, 90, -120]) {
    const box = { x: 80, y: 60, width: 240, height: 120, rotation }
    for (const [cx, cy] of [
      [0, 0],
      [1, 0],
      [1, 1],
      [0, 1],
    ]) {
      const fixed = transformPoint(box, (1 - cx) * box.width, (1 - cy) * box.height)
      const pointer = transformPoint(box, cx ? 360 : -120, cy ? 180 : -60)
      const next = resizeFromCorner(box, pointer, cx, cy, true)
      const after = transformPoint(next, (1 - cx) * next.width, (1 - cy) * next.height)
      close(after.x, fixed.x)
      close(after.y, fixed.y)
      close(next.width / next.height, 2)
      close(next.width, 360)
    }
  }
})

test('画板自由缩放并阻止越过对角后翻转', () => {
  const box = { x: 0, y: 0, width: 200, height: 100 }
  const next = resizeFromCorner(box, { x: 300, y: 70 }, 1, 1, false)
  assert.equal(next.width, 300)
  assert.equal(next.height, 70)
  const crossed = resizeFromCorner(box, { x: -50, y: -20 }, 1, 1, false)
  assert.equal(crossed.width, 48)
  assert.equal(crossed.height, 48)
})

test('旋转保持中心位置,旋转包围盒覆盖实际内容', () => {
  const box = { x: 10, y: 20, width: 200, height: 100, rotation: 0 }
  const next = rotateAroundCenter(box, 90)
  const beforeCenter = transformPoint(box, 100, 50)
  const afterCenter = transformPoint(next, 100, 50)
  close(beforeCenter.x, afterCenter.x)
  close(beforeCenter.y, afterCenter.y)
  const bounds = transformBounds(next)
  close(bounds.width, 100)
  close(bounds.height, 200)
})

test('旋转命令支持保存序列化、撤销与重做', () => {
  const before = { x: 10, y: 20, width: 200, height: 100, rotation: 0 }
  const doc = {
    objects: { image: { id: 'image', kind: 'image' as const, ...before } },
    order: ['image'],
  }
  const next = rotateAroundCenter(before, 45)
  const command = cmdUpdateObject('image', next, before)
  applyCommand(doc, command)
  const restored = JSON.parse(JSON.stringify(doc)) as typeof doc
  assert.equal(restored.objects.image.rotation, 45)
  assert.deepEqual(transformBounds(restored.objects.image), transformBounds(next))
  applyCommand(restored, invertCommand(command))
  assert.deepEqual(restored.objects.image, { id: 'image', kind: 'image', ...before })
  applyCommand(restored, command)
  assert.equal(restored.objects.image.rotation, 45)
})

test('左右、上下边中点只调整对应尺寸并固定对侧,支持旋转', () => {
  for (const rotation of [0, 45, 90, -60]) {
    const box = { x: 70, y: 80, width: 200, height: 100, rotation }
    for (const [cx, cy] of [
      [0, 0.5],
      [1, 0.5],
      [0.5, 0],
      [0.5, 1],
    ]) {
      const fixed = transformPoint(box, (1 - cx) * box.width, (1 - cy) * box.height)
      const pointer = transformPoint(
        box,
        cx === 0.5 ? 130 : cx ? 240 : -40,
        cy === 0.5 ? 80 : cy ? 140 : -40,
      )
      const next = resizeFromCorner(box, pointer, cx, cy, false)
      close(next.width, cx === 0.5 ? 200 : 240)
      close(next.height, cy === 0.5 ? 100 : 140)
      const after = transformPoint(next, (1 - cx) * next.width, (1 - cy) * next.height)
      close(after.x, fixed.x)
      close(after.y, fixed.y)
    }
  }
})

test('四角可以独立横向、纵向及双向缩放,保持对角固定', () => {
  for (const rotation of [0, 37, 90]) {
    const box = { x: 70, y: 80, width: 200, height: 100, rotation }
    for (const [cx, cy] of [
      [0, 0],
      [1, 0],
      [1, 1],
      [0, 1],
    ]) {
      for (const [width, height] of [
        [260, 100],
        [200, 160],
        [260, 160],
        [140, 70],
      ]) {
        const fixed = transformPoint(box, (1 - cx) * box.width, (1 - cy) * box.height)
        const pointer = transformPoint(
          { ...box, ...fixed },
          (cx * 2 - 1) * width,
          (cy * 2 - 1) * height,
        )
        const next = resizeFromCorner(box, pointer, cx, cy, false)
        close(next.width, width)
        close(next.height, height)
        const after = transformPoint(next, (1 - cx) * width, (1 - cy) * height)
        close(after.x, fixed.x)
        close(after.y, fixed.y)
      }
    }
  }
})

test('四角和边缘双向光标随旋转匹配实际缩放方向', () => {
  assert.equal(resizeCursor(0, 0, 0), 'nwse-resize')
  assert.equal(resizeCursor(1, 1, 0), 'nwse-resize')
  assert.equal(resizeCursor(1, 0, 0), 'nesw-resize')
  assert.equal(resizeCursor(0, 1, 0), 'nesw-resize')
  assert.equal(resizeCursor(0, 0, 90), 'nesw-resize')
  assert.equal(resizeCursor(1, 0.5, 90), 'ns-resize')
  assert.equal(resizeCursor(0.5, 0, -90), 'ew-resize')
})
