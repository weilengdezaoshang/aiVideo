import './dom.js'
import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  mergeRemoteDocument,
  RemoteDocumentConflict,
} from '../apps/web/canvas/state/remote-document.js'
import { createAutoSaver } from '../apps/web/canvas/state/autosave.js'
import { createDocStore, type CanvasDocData } from '../apps/web/canvas/state/doc-store.js'
import {
  cmdAddObjects,
  cmdRemoveObjects,
  cmdUpdateObject,
} from '../apps/web/canvas/state/commands.js'

function document(): CanvasDocData {
  return {
    id: 'host-doc',
    name: 'test',
    revision: 1,
    objects: {
      image: {
        id: 'image',
        kind: 'image',
        x: 0,
        y: 0,
        width: 200,
        height: 200,
        assetId: 'old',
        assetVersion: 1,
      },
    },
    order: ['image'],
  }
}
test('远端 Host 产物和本地布局修改合并，同字段修改/删除冲突不覆盖', () => {
  const base = document(),
    local = structuredClone(base),
    remote = structuredClone(base)
  local.objects.image.x = 80
  remote.revision = 2
  remote.objects.image.assetId = 'new'
  remote.objects.image.assetVersion = 2
  remote.objects.generated = {
    id: 'generated',
    kind: 'image',
    x: 300,
    y: 0,
    width: 200,
    height: 200,
    createdByRunId: 'run',
    assetId: 'generated',
    badge: 'preview',
  }
  remote.order.push('generated')
  const merged = mergeRemoteDocument(base, local, remote)
  assert.equal(merged.objects.image.x, 80)
  assert.equal(merged.objects.image.assetId, 'new')
  assert.equal(merged.revision, 2)
  assert.deepEqual(merged.order, ['image', 'generated'])
  local.objects.image.assetId = 'another-local-image'
  assert.throws(() => mergeRemoteDocument(base, local, remote), RemoteDocumentConflict)
  local.objects.image.assetId = 'old'
  delete remote.objects.image
  remote.order = ['generated']
  assert.throws(() => mergeRemoteDocument(base, local, remote), RemoteDocumentConflict)
})
test('Host 同步采用新 revision，下一次保存保留本地布局和新增产物', async () => {
  const initial = document()
  const store = createDocStore(initial)
  const remote = structuredClone(initial)
  remote.revision = 2
  remote.objects.generated = {
    id: 'generated',
    kind: 'image',
    x: 300,
    y: 0,
    width: 200,
    height: 200,
    createdByRunId: 'run',
    assetId: 'generated',
    badge: 'preview',
  }
  remote.order.push('generated')
  const saved: Record<string, unknown>[] = []
  const saver = createAutoSaver({
    docId: initial.id,
    getDoc: () => store.doc,
    debounceMs: 10000,
    onRemoteDocument: (merged) => store.mutateTransient((doc) => Object.assign(doc, merged)),
    fetchImpl: async (_url, init) => {
      if (init?.method === 'POST') {
        saved.push(JSON.parse(String(init.body)) as Record<string, unknown>)
        return Response.json({ document: { ...remote, revision: 3 } })
      }
      return Response.json({ document: remote })
    },
  })
  store.apply(cmdUpdateObject('image', { x: 80 }, { x: 0 }))
  saver.markDirty()
  await saver.syncRemote()
  assert.equal(store.doc.revision, 2)
  assert.equal(store.doc.objects.image.x, 80)
  assert.ok(store.doc.objects.generated)
  await saver.ensureSaved()
  assert.equal(saved[0].baseRevision, 2)
  assert.equal((saved[0].objects as CanvasDocData['objects']).image.x, 80)
  assert.equal((saved[0].objects as CanvasDocData['objects']).generated.assetId, 'generated')
})
test('busy 节点禁止删除、替换和撤销创建，移动仍可撤销', () => {
  const store = createDocStore({ id: 'd', name: '', objects: {}, order: [] })
  const node = { id: 'busy', kind: 'placeholder' as const, x: 0, y: 0, width: 200, height: 200 }
  store.apply(cmdAddObjects([node]))
  store.mutateTransient((doc) => {
    doc.objects.busy.busyRunId = 'run'
  })
  assert.equal(store.canUndo(), false)
  assert.equal(store.undo(), false)
  assert.throws(() => store.apply(cmdRemoveObjects([{ object: node, orderIndex: 0 }])), /node_busy/)
  assert.throws(
    () => store.apply(cmdUpdateObject('busy', { kind: 'image' }, { kind: 'placeholder' })),
    /node_busy/,
  )
  store.apply(cmdUpdateObject('busy', { x: 40 }, { x: 0 }))
  assert.equal(store.undo(), true)
  assert.equal(store.doc.objects.busy.x, 0)
  store.mutateTransient((doc) => {
    delete doc.objects.busy.busyRunId
  })
  assert.equal(store.undo(), true)
  assert.equal(store.doc.objects.busy, undefined)
})
