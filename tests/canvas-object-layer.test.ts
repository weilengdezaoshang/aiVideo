import test from 'node:test'
import assert from 'node:assert/strict'
import { syncSelectionRectPositions } from '../web/canvas/engine/object-layer.js'

test('拖动期间选中框跟随节点实时坐标，而不是停留在提交前坐标', () => {
  const selected = new Set(['image-1', 'image-2'])
  const positions = new Map<string, { x: number; y: number }>()
  let drawCount = 0

  const rect = (id: string) => ({
    position: (point: { x: number; y: number }) => positions.set(id, point),
  })
  const node = (x: number, y: number) => ({
    x: () => x,
    y: () => y,
  })

  syncSelectionRectPositions(
    selected,
    new Map([
      ['image-1', rect('image-1')],
      ['image-2', rect('image-2')],
    ]),
    new Map([
      ['image-1', node(180, 240)],
      ['image-2', node(520, 260)],
    ]),
    { batchDraw: () => drawCount++ },
  )

  assert.deepEqual(positions.get('image-1'), { x: 180, y: 240 })
  assert.deepEqual(positions.get('image-2'), { x: 520, y: 260 }, '多选中的其他图形也应同步')
  assert.equal(drawCount, 1, '坐标同步后重绘一次覆盖层')
})
