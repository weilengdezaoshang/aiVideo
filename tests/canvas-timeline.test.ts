import test from 'node:test'
import assert from 'node:assert/strict'
import {
  emptyTimeline,
  splitClip,
  trimClip,
  moveClip,
  timelineDuration,
  clipAtFrame,
  type Timeline,
} from '../apps/web/canvas/state/timeline.js'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'
import { draftMatchesDocument } from '../apps/web/canvas/state/document-api.js'
const initial = (): Timeline => ({
  ...emptyTimeline(),
  clips: [
    {
      id: 'a',
      name: '镜头',
      source: { nodeId: 'node-a', kind: 'video', url: '/images/movie.mp4', durationFrames: 300 },
      inFrame: 30,
      outFrame: 210,
    },
  ],
})

test('分割保留非零入点、连续源帧和总时长；移动不改变裁剪', () => {
  const result = splitClip(initial(), 60, 'b')
  assert.equal(timelineDuration(result), 180)
  assert.deepEqual(
    result.clips.map((c) => [c.inFrame, c.outFrame]),
    [
      [30, 90],
      [90, 210],
    ],
  )
  assert.equal(clipAtFrame(result, 59)?.sourceFrame, 89)
  assert.equal(clipAtFrame(result, 60)?.sourceFrame, 90)
  assert.equal(clipAtFrame(result, 180), null)
  assert.deepEqual(
    moveClip(result, 'b', 0).clips.map((c) => c.id),
    ['b', 'a'],
  )
})
test('拒绝越界、空片段、小数帧、边界分割和重复 ID', () => {
  for (const [a, b] of [
    [-1, 30],
    [50, 50],
    [0, 301],
    [0, NaN],
    [0.5, 30],
  ]) {
    assert.throws(() => trimClip(initial(), 'a', a, b))
  }
  assert.throws(() => splitClip(initial(), 0, 'b'))
  assert.throws(() => splitClip(initial(), 180, 'b'))
  assert.throws(() => splitClip(initial(), 30, 'a'))
})
test('时间线命令可撤销到旧文档并重做，命令和源素材快照互不污染', () => {
  const store = createDocStore({ id: 'doc', name: '测试', objects: {}, order: [] })
  const tl = initial()
  store.apply({ type: 'setTimeline', after: tl })
  store.doc.timeline!.clips[0].name = '瞬态修改'
  assert.equal(tl.clips[0].name, '镜头')
  store.undo()
  assert.equal(store.doc.timeline, undefined)
  store.redo()
  assert.equal(store.doc.timeline!.clips[0].name, '镜头')
  assert.equal(store.doc.timeline!.clips[0].source.url, '/images/movie.mp4')
  assert.equal(
    draftMatchesDocument(store.doc, { ...store.doc, timeline: trimClip(initial(), 'a', 40, 200) }),
    false,
  )
})
