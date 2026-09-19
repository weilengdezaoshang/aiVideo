import test from 'node:test'
import assert from 'node:assert/strict'
import { connectReference } from '../apps/web/canvas/flows/connect-reference.js'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'
import { createNode } from '../apps/web/canvas/state/node-model.js'

test('端口连接写入生成参考，拒绝重复并支持撤销', async () => {
  const source = createNode('image', { x: 0, y: 0 }, '产品')
  source.assetId = 'asset'
  const target = createNode('video', { x: 400, y: 0 }, '镜头')
  const store = createDocStore({
    id: 'doc',
    name: 'doc',
    objects: { [source.id]: source, [target.id]: target },
    order: [source.id, target.id],
  })
  const fetchBefore = globalThis.fetch
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ video: { supported: true, referenceLimit: 1 } }))
  try {
    await connectReference(store, source.id, target.id, new AbortController().signal)
    assert.equal(store.doc.objects[target.id].nodeDraft!.references[0].sourceNodeId, source.id)
    await assert.rejects(
      connectReference(store, source.id, target.id, new AbortController().signal),
      /已被引用/,
    )
    store.undo()
    assert.equal(store.doc.objects[target.id].nodeDraft!.references.length, 0)
    const controller = new AbortController()
    controller.abort()
    await assert.rejects(connectReference(store, source.id, target.id, controller.signal))
    assert.equal(store.doc.objects[target.id].nodeDraft!.references.length, 0)
  } finally {
    globalThis.fetch = fetchBefore
  }
})

test('并发连接不会突破模型上限或覆盖先完成的引用', async () => {
  const first = createNode('image', { x: 0, y: 0 }, '产品')
  first.assetId = 'asset-first'
  const second = createNode('image', { x: 0, y: 300 }, '场景')
  second.assetId = 'asset-second'
  const target = createNode('video', { x: 400, y: 0 }, '镜头')
  const store = createDocStore({
    id: 'doc',
    name: 'doc',
    objects: { [first.id]: first, [second.id]: second, [target.id]: target },
    order: [first.id, second.id, target.id],
  })
  const fetchBefore = globalThis.fetch
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ video: { supported: true, referenceLimit: 1 } }))
  try {
    const outcomes = await Promise.allSettled([
      connectReference(store, first.id, target.id, new AbortController().signal),
      connectReference(store, second.id, target.id, new AbortController().signal),
    ])
    assert.equal(outcomes.filter((outcome) => outcome.status === 'fulfilled').length, 1)
    assert.equal(store.doc.objects[target.id].nodeDraft!.references.length, 1)
    store.undo()
    assert.equal(store.doc.objects[target.id].nodeDraft!.references.length, 0)
  } finally {
    globalThis.fetch = fetchBefore
  }
})
