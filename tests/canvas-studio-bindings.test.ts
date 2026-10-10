import assert from 'node:assert/strict'
import test from 'node:test'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'
import { defaultDraft } from '../apps/web/canvas/state/node-model.js'
import {
  detachReference,
  mediaSource,
  saveNodeDraft,
  timelineNodeStore,
} from '../apps/web/canvas/state/studio-bindings.js'
import { emptyTimeline } from '../apps/web/canvas/state/timeline.js'

function fixture() {
  return createDocStore({
    id: 'doc',
    name: '测试',
    objects: {
      a: {
        id: 'a',
        kind: 'placeholder',
        placeholderType: 'image',
        nodeDraft: defaultDraft('image'),
        x: 0,
        y: 0,
        width: 320,
        height: 320,
      },
      b: {
        id: 'b',
        kind: 'placeholder',
        placeholderType: 'timeline',
        timeline: emptyTimeline(),
        x: 400,
        y: 0,
        width: 480,
        height: 160,
      },
      c: {
        id: 'c',
        kind: 'placeholder',
        placeholderType: 'timeline',
        timeline: emptyTimeline(),
        x: 400,
        y: 200,
        width: 480,
        height: 160,
      },
    },
    order: ['a', 'b', 'c'],
  })
}

test('节点草稿独立保存，可撤销，生成提交后不再改写输入快照', () => {
  const store = fixture()
  saveNodeDraft(store, 'a', { ...defaultDraft('image'), prompt: '山间小屋', model: 'mock' })
  assert.equal(store.doc.objects.a.nodeDraft?.prompt, '山间小屋')
  store.undo()
  assert.equal(store.doc.objects.a.nodeDraft?.prompt, '')
  store.doc.objects.a.generationId = 'submitted'
  saveNodeDraft(store, 'a', { ...defaultDraft('image'), prompt: '不能改写' })
  assert.equal(store.doc.objects.a.nodeDraft?.prompt, '')
})

test('两个时间线占位符各自保存剪辑，撤销与重做只影响目标节点', () => {
  const store = fixture()
  const first = timelineNodeStore(store, 'b')
  const second = timelineNodeStore(store, 'c')
  first.apply({
    type: 'setTimeline',
    before: first.doc.timeline,
    after: {
      ...emptyTimeline(),
      clips: [
        {
          id: 'clip',
          name: '素材',
          source: { nodeId: 'a', kind: 'image', url: '/api/assets/id/content', durationFrames: 90 },
          inFrame: 0,
          outFrame: 90,
        },
      ],
    },
  })
  assert.equal(first.doc.timeline?.clips.length, 1)
  assert.equal(second.doc.timeline?.clips.length, 0)
  assert.equal(store.doc.timeline, undefined)
  store.undo()
  assert.equal(first.doc.timeline?.clips.length, 0)
  store.redo()
  assert.equal(first.doc.timeline?.clips.length, 1)
  assert.equal(second.doc.timeline?.clips.length, 0)
})

test('断开引用可撤销，已提交节点拒绝断开，导出使用授权资产地址', () => {
  const store = fixture()
  store.doc.objects.a.nodeDraft!.references = [
    { assetId: 'asset', ext: 'png', name: '参考', sourceNodeId: 'source' },
  ]
  detachReference(store, 'a', 'source')
  assert.equal(store.doc.objects.a.nodeDraft?.references.length, 0)
  store.undo()
  assert.equal(store.doc.objects.a.nodeDraft?.references.length, 1)
  store.doc.objects.a.generationId = 'running'
  assert.throws(() => detachReference(store, 'a', 'source'))
  assert.equal(
    mediaSource({ ...store.doc.objects.a, assetId: 'owned' }),
    '/api/assets/owned/content',
  )
})

test('原位生成撤销后恢复原占位符及草稿，重做仍使用同一生成实体', async () => {
  const { cmdGenerateNode } = await import('../apps/web/canvas/state/commands.js')
  const store = fixture()
  const previous = structuredClone(store.doc.objects.a)
  store.apply(
    cmdGenerateNode({
      transactionId: 'tx',
      objectId: 'a',
      generationId: 'gen',
      previousPlacement: previous,
      initialPlacement: {
        ...previous,
        generationId: 'gen',
        nodeDraft: undefined,
        placeholderType: undefined,
      },
    }),
  )
  assert.equal(store.doc.objects.a.generationId, 'gen')
  store.undo()
  assert.deepEqual(store.doc.objects.a, previous)
  store.redo()
  assert.equal(store.doc.objects.a.generationId, 'gen')
})

test('成品素材后续编辑草稿独立保存，不改变原生成参数', () => {
  const store = fixture()
  store.doc.objects.a = {
    ...store.doc.objects.a,
    kind: 'image',
    assetId: 'asset',
    generationId: 'finished',
    generationSpec: { mode: 'text-to-image', params: { prompt: '原始输入' } },
  }
  saveNodeDraft(store, 'a', { ...defaultDraft('image'), prompt: '下一次编辑' })
  assert.equal(store.doc.objects.a.nodeDraft?.prompt, '下一次编辑')
  assert.equal(store.doc.objects.a.generationSpec?.params.prompt, '原始输入')
  store.undo()
  assert.equal(store.doc.objects.a.nodeDraft?.prompt, '')
})
