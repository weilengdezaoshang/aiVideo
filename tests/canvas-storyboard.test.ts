import test from 'node:test'
import assert from 'node:assert/strict'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'
import { emptyStoryboard, newShot } from '../apps/web/canvas/state/storyboard.js'

test('分镜命令保存独立快照并支持撤销重做', () => {
  const store = createDocStore({ id: 'doc', name: 'doc', objects: {}, order: [] })
  const board = emptyStoryboard()
  board.shots.push(newShot())
  store.apply({ type: 'setStoryboard', after: board })
  board.shots[0].visual = '外部更改'
  assert.equal(store.doc.storyboard!.shots[0].visual, '')
  store.undo()
  assert.equal(store.doc.storyboard, undefined)
  store.redo()
  assert.equal(store.doc.storyboard!.shots.length, 1)
})
