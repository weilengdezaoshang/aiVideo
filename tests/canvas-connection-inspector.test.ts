import './dom.js'
import test from 'node:test'
import assert from 'node:assert/strict'
import { fireEvent, screen } from '@testing-library/dom'
import { testWindow } from './dom.js'
import { createConnectionInspector } from '../apps/web/canvas/flows/connection-inspector.js'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'
import { createNode } from '../apps/web/canvas/state/node-model.js'
import { cmdUpdateObject } from '../apps/web/canvas/state/commands.js'

testWindow.HTMLDialogElement.prototype.showModal = function () {
  this.open = true
}
testWindow.HTMLDialogElement.prototype.close = function () {
  this.open = false
}

test('连线详情断开真实引用，可撤销，弹窗键盘不影响画布', () => {
  const source = createNode('image', { x: 0, y: 0 }, '<img src=x>')
  const target = createNode('video', { x: 400, y: 0 }, '镜头')
  target.nodeDraft!.references = [
    { assetId: 'asset', ext: 'png', name: '参考', sourceNodeId: source.id },
  ]
  const store = createDocStore({
    id: 'test',
    name: 'test',
    objects: { [source.id]: source, [target.id]: target },
    order: [source.id, target.id],
  })
  const view = createConnectionInspector(store)
  const edge = { sourceId: source.id, targetId: target.id, kind: 'reference' as const }
  let keys = 0
  const key = () => {
    keys++
  }
  document.addEventListener('keydown', key)
  try {
    view.inspect(edge)
    assert.equal(screen.getByRole('dialog').querySelector('img'), null)
    fireEvent.keyDown(screen.getByRole('button', { name: '断开引用' }), { key: 'Delete' })
    assert.equal(keys, 0)
    fireEvent.click(screen.getByRole('button', { name: '断开引用' }))
    assert.equal(store.doc.objects[target.id].nodeDraft!.references.length, 0)
    store.undo()
    assert.equal(store.doc.objects[target.id].nodeDraft!.references.length, 1)
    view.inspect(edge)
    const remove = screen.getByRole('button', { name: '断开引用' })
    store.apply(
      cmdUpdateObject(
        target.id,
        {
          nodeRun: {
            requestId: 'run',
            status: 'queued',
            snapshot: structuredClone(target.nodeDraft!),
          },
        },
        {},
      ),
    )
    fireEvent.click(remove)
    assert.equal(store.doc.objects[target.id].nodeDraft!.references.length, 1)
    view.inspect(edge)
    assert.equal(screen.queryByRole('button', { name: '断开引用' }), null)
    view.inspect({ ...edge, kind: 'lineage' })
    assert.match(screen.getByRole('dialog').textContent!, /历史来源不可断开/)
  } finally {
    document.removeEventListener('keydown', key)
    view.dispose()
  }
  assert.equal(document.querySelector('dialog'), null)
})
