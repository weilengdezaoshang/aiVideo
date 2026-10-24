import test from 'node:test'
import assert from 'node:assert/strict'
import { createHistoryManager } from '../apps/web/canvas/state/history-manager.js'
import {
  cmdMoveObjects,
  cmdRemoveObjects,
  cmdUpdateObject,
  type CanvasObj,
  type CommandDoc,
} from '../apps/web/canvas/state/commands.js'
import { createCanvasView } from '../apps/web/canvas/core/canvas-view.js'
import type { CanvasInputEvent } from '../apps/web/canvas/core/types.js'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'

function opened(): CommandDoc {
  const group: CanvasObj = {
    id: 'poster',
    kind: 'board',
    layerRole: 'group',
    x: 10,
    y: 20,
    width: 100,
    height: 200,
  }
  const text: CanvasObj = {
    id: 'title',
    kind: 'text',
    groupId: 'poster',
    layerRole: 'text',
    x: 20,
    y: 30,
    width: 80,
    height: 40,
    text: '月圆人团圆',
    fontSize: 20,
    lineBoxes: [{ text: '月圆人团圆', x: 0, y: 0, width: 80, height: 20 }],
  }
  return { objects: { poster: group, title: text }, order: ['poster', 'title'] }
}

test('actual history intake moves a poster and its text; undo, redo and reload preserve both', () => {
  const doc = opened()
  const before = structuredClone(doc)
  const history = createHistoryManager(doc)
  history.apply(cmdMoveObjects([{ id: 'poster', from: { x: 10, y: 20 }, to: { x: 30, y: 50 } }]))
  assert.equal(doc.objects.title.x, 40)
  assert.equal(doc.objects.title.y, 60)
  const saved: CommandDoc = JSON.parse(JSON.stringify(doc)) as CommandDoc
  assert.equal(saved.objects.title.text, '月圆人团圆')
  assert(history.undo())
  assert.deepEqual(doc, before)
  assert(history.redo())
  assert.deepEqual(doc, saved)
})

test('portrait resize scales children and text uniformly, then undo restores exact line boxes', () => {
  const doc = opened()
  const before = structuredClone(doc)
  const history = createHistoryManager(doc)
  history.apply(cmdUpdateObject('poster', { width: 200, height: 400 }, { width: 100, height: 200 }))
  assert.equal(doc.objects.title.x, 30)
  assert.equal(doc.objects.title.y, 40)
  assert.equal(doc.objects.title.fontSize, 40)
  assert.equal(doc.objects.title.lineBoxes?.[0].width, 160)
  assert(history.undo())
  assert.deepEqual(doc, before)
})

test('deleting a poster removes all layers and a single undo restores the complete group', () => {
  const doc = opened()
  const before = structuredClone(doc)
  const history = createHistoryManager(doc)
  history.apply(cmdRemoveObjects([{ object: structuredClone(doc.objects.poster), orderIndex: 0 }]))
  assert.deepEqual(doc.order, [])
  assert.deepEqual(doc.objects, {})
  assert(history.undo())
  assert.deepEqual(doc, before)
})

test('explicitly selected group children do not receive movement twice', () => {
  const doc = opened()
  const history = createHistoryManager(doc)
  history.apply(
    cmdMoveObjects([
      { id: 'poster', from: { x: 10, y: 20 }, to: { x: 20, y: 30 } },
      { id: 'title', from: { x: 20, y: 30 }, to: { x: 30, y: 40 } },
    ]),
  )
  assert.equal(doc.objects.title.x, 30)
  assert.equal(doc.objects.title.y, 40)
})

test('a real drag previews every layer, clears the preview, commits once and can undo', () => {
  const data = opened()
  const store = createDocStore({ id: 'doc', name: 'poster', ...data })
  const before = structuredClone(store.doc.objects)
  const view = createCanvasView({ store })
  const pointer = (
    type: 'lbutton-down' | 'move' | 'lbutton-up',
    x: number,
    y: number,
  ): CanvasInputEvent => ({
    type,
    pointerId: 1,
    button: 0,
    screenPoint: { x, y },
    worldPoint: { x, y },
    modifiers: { shift: false, alt: false, ctrl: false, meta: false, space: false },
    target: { kind: 'object', objectId: 'poster' },
  })
  view.dispatchInput(pointer('lbutton-down', 10, 20))
  view.dispatchInput(pointer('move', 30, 50))
  assert.equal(view.getSnapshot().objectPreviews.title.x, 40)
  assert.equal(view.getSnapshot().objectPreviews.title.y, 60)
  assert.deepEqual(store.doc.objects, before)
  view.dispatchInput(pointer('lbutton-up', 30, 50))
  assert.deepEqual(view.getSnapshot().objectPreviews, {})
  assert.equal(store.doc.objects.title.x, 40)
  assert.equal(store.doc.objects.title.y, 60)
  assert(view.undo())
  assert.deepEqual(store.doc.objects, before)
  view.destroy()
})
