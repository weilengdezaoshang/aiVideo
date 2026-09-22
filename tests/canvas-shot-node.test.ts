import test from 'node:test'
import assert from 'node:assert/strict'
import {
  createMissingShotNodes,
  createShotNode,
  createShotVideoVersions,
} from '../apps/web/canvas/flows/shot-node.js'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'
import { newShot } from '../apps/web/canvas/state/storyboard.js'
import { createNode } from '../apps/web/canvas/state/node-model.js'

test('镜头创建节点保留图片参考，撤销同时恢复关联且不提交任务', () => {
  const source = createNode('image', { x: 0, y: 0 }, '产品')
  source.assetId = 'asset'
  const shot = { ...newShot(), visual: '产品旋转', camera: '缓慢推近', nodeId: source.id }
  const store = createDocStore({
    id: 'doc',
    name: 'doc',
    objects: { [source.id]: source },
    order: [source.id],
    storyboard: { version: 1, outline: '', shots: [shot] },
  })
  const id = createShotNode(store, shot.id, 'video')
  assert.equal(store.doc.storyboard!.shots[0].nodeId, id)
  assert.deepEqual(store.doc.storyboard!.shots[0].versions, [source.id, id])
  assert.match(store.doc.objects[id].nodeDraft!.prompt, /产品旋转\n运镜：缓慢推近/)
  assert.equal(store.doc.objects[id].nodeDraft!.references[0].assetId, 'asset')
  assert.equal(store.doc.objects[id].nodeRun, undefined)
  store.undo()
  assert.equal(store.doc.objects[id], undefined)
  assert.equal(store.doc.storyboard!.shots[0].nodeId, source.id)
  store.redo()
  assert.equal(store.doc.storyboard!.shots[0].nodeId, id)
  store.doc.storyboard!.shots[0].locked = true
  assert.throws(() => createShotNode(store, shot.id, 'image'), /锁定/)
})

test('批量补齐跳过锁定及已有节点，一次撤销整批且校验失败不留半成品', () => {
  const existing = createNode('image', { x: 0, y: 0 }, '已有素材')
  const shots = [
    { ...newShot(), visual: '保留', nodeId: existing.id },
    { ...newShot(), visual: '锁定', locked: true },
    { ...newShot(), visual: '开场' },
    { ...newShot(), title: '结尾', visual: '', nodeId: 'deleted' },
  ]
  const store = createDocStore({
    id: 'batch',
    name: 'doc',
    objects: { [existing.id]: existing },
    order: [existing.id],
    storyboard: { version: 1, outline: '', shots },
  })
  const before = structuredClone(store.doc)
  assert.throws(() => createMissingShotNodes(store, 'video'), /结尾.*画面描述/)
  assert.deepEqual(store.doc, before)
  shots[3].visual = '落幕'
  const ids = createMissingShotNodes(store, 'video')
  assert.equal(ids.length, 2)
  assert.equal(store.doc.storyboard!.shots[0].nodeId, existing.id)
  assert.equal(store.doc.storyboard!.shots[1].nodeId, null)
  for (const id of ids) {
    assert.equal(store.doc.objects[id].kind, 'video')
    assert.equal(store.doc.objects[id].nodeRun, undefined)
  }
  assert.notEqual(store.doc.objects[ids[0]].x, store.doc.objects[ids[1]].x)
  store.undo()
  assert.deepEqual(store.doc.order, [existing.id])
  assert.equal(store.doc.storyboard!.shots[3].nodeId, 'deleted')
  store.redo()
  assert.deepEqual(store.doc.order, [existing.id, ...ids])
})

test('批量图片转视频导入历史图片并保留参考和原版本，可整体撤销', async () => {
  const image = createNode('image', { x: 0, y: 0 }, '历史图')
  image.src = '/history.png'
  const shot = { ...newShot(), visual: '人物向前走', nodeId: image.id }
  const store = createDocStore({
    id: 'image-video',
    name: 'doc',
    objects: { [image.id]: image },
    order: [image.id],
    storyboard: { version: 1, outline: '', shots: [shot] },
  })
  const ids = await createShotVideoVersions(store, new AbortController().signal, async (source) => {
    assert.equal(source.src, '/history.png')
    return { id: 'imported', ext: 'png' }
  })
  const video = store.doc.objects[ids[0]]
  assert.equal(video.kind, 'video')
  assert.equal(video.nodeDraft!.references[0].assetId, 'imported')
  assert.equal(video.nodeDraft!.references[0].sourceNodeId, image.id)
  assert.deepEqual(store.doc.storyboard!.shots[0].versions, [image.id, video.id])
  assert.equal(video.nodeRun, undefined)
  assert.equal(store.doc.objects[image.id].src, '/history.png')
  store.undo()
  assert.equal(store.doc.storyboard!.shots[0].nodeId, image.id)
  assert.equal(store.doc.objects[video.id], undefined)
})

test('图片入库期间分镜变化时不会覆盖用户新选择', async () => {
  const image = createNode('image', { x: 0, y: 0 }, '图')
  image.src = '/history.png'
  const shot = { ...newShot(), visual: '远景', nodeId: image.id }
  const store = createDocStore({
    id: 'stale-image-video',
    name: 'doc',
    objects: { [image.id]: image },
    order: [image.id],
    storyboard: { version: 1, outline: '', shots: [shot] },
  })
  await assert.rejects(
    createShotVideoVersions(store, new AbortController().signal, async () => {
      store.doc.storyboard!.shots[0].locked = true
      return { id: 'imported', ext: 'png' }
    }),
    /已变化/,
  )
  assert.deepEqual(store.doc.order, [image.id])
  assert.equal(store.doc.storyboard!.shots[0].locked, true)
})
