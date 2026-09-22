import test from 'node:test'
import assert from 'node:assert/strict'
import { applySubjectToShots } from '../apps/web/canvas/flows/shot-subject.js'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'
import { createNode } from '../apps/web/canvas/state/node-model.js'
import { newShot } from '../apps/web/canvas/state/storyboard.js'

function fixture() {
  const subject = createNode('image', { x: 0, y: 0 }, '主体')
  subject.src = '/images/subject.png'
  subject.name = '产品瓶'
  const done = createNode('image', { x: 400, y: 0 }, '已完成')
  done.src = '/images/done.png'
  const pending = createNode('image', { x: 800, y: 0 }, '待生成')
  const withRef = createNode('image', { x: 1200, y: 0 }, '已有参考')
  withRef.nodeDraft!.references = [
    { assetId: 'asset-other', ext: 'png', name: '已有', sourceNodeId: done.id },
  ]
  const store = createDocStore({
    id: 'subject',
    name: 'doc',
    objects: Object.fromEntries([subject, done, pending, withRef].map((node) => [node.id, node])),
    order: [subject.id, done.id, pending.id, withRef.id],
    storyboard: {
      version: 1,
      outline: '',
      shots: [
        { ...newShot(), title: '一', visual: '主体特写', nodeId: subject.id },
        { ...newShot(), title: '二', visual: '场景', nodeId: done.id },
        { ...newShot(), title: '三', visual: '场景', nodeId: pending.id },
        { ...newShot(), title: '四', visual: '场景', nodeId: withRef.id },
        { ...newShot(), title: '五', visual: '场景', nodeId: null },
        { ...newShot(), title: '六', visual: '场景', nodeId: pending.id, locked: true },
      ],
    },
  })
  return { store, subject, done, pending, withRef }
}

test('主体一次引用到全部待生成镜头，跳过已完成/已有参考/未关联/锁定，整批一次撤销', async () => {
  const { store, subject, pending, withRef } = fixture()
  const imported: unknown[] = []
  const result = await applySubjectToShots(
    store,
    subject,
    new AbortController().signal,
    async (obj) => {
      imported.push(obj)
      return { id: 'asset-subject', ext: 'png' }
    },
  )
  assert.equal(result.applied, 1, '只有镜头三的待生成节点可应用')
  assert.ok(result.skipped.some((item) => item.includes('镜头 1') && item.includes('已有素材')))
  assert.ok(result.skipped.some((item) => item.includes('镜头 2') && item.includes('已有素材')))
  assert.ok(result.skipped.some((item) => item.includes('镜头 4') && item.includes('已携带参考图')))
  assert.ok(
    result.skipped.some((item) => item.includes('镜头 5') && item.includes('没有可生成的节点')),
  )
  assert.ok(result.skipped.some((item) => item.includes('镜头 6') && item.includes('已锁定')))
  assert.deepEqual(imported, [subject], 'src 主体先入库一次')
  const draft = store.doc.objects[pending.id].nodeDraft!
  assert.equal(draft.references[0].assetId, 'asset-subject')
  assert.equal(draft.references[0].name, '产品瓶')
  assert.equal(withRef.nodeDraft!.references[0].assetId, 'asset-other', '已有参考不被覆盖')
  // 整批一次撤销
  store.undo()
  assert.equal(store.doc.objects[pending.id].nodeDraft!.references.length, 0)
})

test('素材库主体（已有 assetId）不再入库，引用直接写入', async () => {
  const { store, pending } = fixture()
  const subject = createNode('image', { x: -400, y: 0 }, '库主体')
  subject.assetId = 'asset-lib'
  subject.ext = 'png'
  store.apply({ type: 'addObjects', objects: [subject] })
  const stored = store.doc.objects[subject.id]
  let imports = 0
  const result = await applySubjectToShots(
    store,
    stored,
    new AbortController().signal,
    async () => {
      imports++
      return { id: subject.assetId!, ext: 'png' }
    },
  )
  assert.equal(result.applied, 1)
  assert.equal(imports, 0, '已有 assetId 不得重复入库')
  assert.equal(store.doc.objects[pending.id].nodeDraft!.references[0].assetId, 'asset-lib')
})

test('异步入库期间分镜变化或主体变化时放弃全部写入', async () => {
  const { store, subject, pending } = fixture()
  const signal = new AbortController().signal
  await assert.rejects(
    applySubjectToShots(store, subject, signal, async () => {
      const board = structuredClone(store.doc.storyboard)!
      board.shots[2].title = '已改名'
      store.apply({ type: 'setStoryboard', before: store.doc.storyboard, after: board })
      return { id: 'asset-subject', ext: 'png' }
    }),
    /已变化/,
  )
  assert.equal(store.doc.objects[pending.id].nodeDraft!.references.length, 0)
})

test('非图片或未生成素材不能作为主体', async () => {
  const { store, done, pending } = fixture()
  const draft = createNode('image', { x: 1600, y: 0 }, '草稿')
  await assert.rejects(applySubjectToShots(store, draft, new AbortController().signal), /请先选择/)
  const video = createNode('video', { x: 2000, y: 0 }, '视频')
  video.src = '/images/v.webp'
  await assert.rejects(
    applySubjectToShots(store, video, new AbortController().signal),
    /仅支持图片/,
  )
  await assert.rejects(
    applySubjectToShots(store, undefined, new AbortController().signal),
    /请先选择/,
  )
  assert.equal(store.doc.objects[pending.id].nodeDraft!.references.length, 0)
  assert.equal(store.doc.objects[done.id].nodeDraft!.references.length, 0)
})
