import test from 'node:test'
import assert from 'node:assert/strict'
import {
  applyCommand,
  cmdAddObjects,
  cmdBatch,
  cmdMoveObjects,
  cmdUpdateObject,
  invertCommand,
} from '../apps/web/canvas/state/commands.js'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'

interface LooseObj {
  id: string
  kind: 'image'
  x: number
  y: number
  width: number
  height: number
  groupId?: string
}

function makeDoc(objects: LooseObj[], order: string[]) {
  return { objects: Object.fromEntries(objects.map((o) => [o.id, o])), order }
}

test('cmdBatch:顺序应用,一次撤销同时回滚派生对象与顺延位移', () => {
  const doc = makeDoc(
    [
      { id: 'src', kind: 'image', x: 0, y: 0, width: 100, height: 100, groupId: 'g1' },
      { id: 'right', kind: 'image', x: 140, y: 0, width: 100, height: 100, groupId: 'g1' },
    ],
    ['src', 'right'],
  )
  const batch = cmdBatch([
    cmdAddObjects([
      { id: 'derived', kind: 'image', x: 124, y: 0, width: 100, height: 100, groupId: 'g1' },
    ]),
    cmdMoveObjects([{ id: 'right', from: { x: 140, y: 0 }, to: { x: 264, y: 0 } }]),
  ])
  applyCommand(doc, batch)
  assert.equal((doc.objects.derived as LooseObj | undefined)?.x, 124)
  assert.equal((doc.objects.right as LooseObj).x, 264)

  // 一次撤销:派生对象移除、顺延还原(A09)
  applyCommand(doc, invertCommand(batch))
  assert.equal(doc.objects.derived, undefined)
  assert.equal((doc.objects.right as LooseObj).x, 140)
  assert.ok(!doc.order.includes('derived'))

  // 重做恢复
  applyCommand(doc, batch)
  assert.equal((doc.objects.derived as LooseObj | undefined)?.x, 124)
  assert.equal((doc.objects.right as LooseObj).x, 264)
})

test('cmdBatch:含 updateObject 时撤销恢复原尺寸', () => {
  const doc = makeDoc([{ id: 'a', kind: 'image', x: 0, y: 0, width: 100, height: 100 }], ['a'])
  const batch = cmdBatch([
    cmdMoveObjects([{ id: 'a', from: { x: 0, y: 0 }, to: { x: 0, y: 0 } }]),
    cmdUpdateObject('a', { width: 220, height: 220 }, { width: 100, height: 100 }),
  ])
  applyCommand(doc, batch)
  assert.equal((doc.objects.a as LooseObj).width, 220)
  applyCommand(doc, invertCommand(batch))
  assert.equal((doc.objects.a as LooseObj).width, 100)
})

test('DocStore:批量命令整体进撤销栈,undo/redo 一步到位', () => {
  const store = createDocStore({
    id: 'd',
    name: 'n',
    revision: 1,
    objects: {},
    order: [],
  })
  store.apply(
    cmdBatch([
      cmdAddObjects([{ id: 'x', kind: 'image', x: 1, y: 1, width: 10, height: 10 }]),
      cmdMoveObjects([{ id: 'x', from: { x: 1, y: 1 }, to: { x: 50, y: 50 } }]),
    ]),
  )
  assert.equal((store.doc.objects.x as LooseObj | undefined)?.x, 50)
  assert.equal(store.undo(), true)
  assert.equal(store.doc.objects.x, undefined)
  assert.equal(store.redo(), true)
  assert.equal((store.doc.objects.x as LooseObj | undefined)?.x, 50)
})
