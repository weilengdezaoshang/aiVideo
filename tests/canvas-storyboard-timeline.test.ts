import test from 'node:test'
import assert from 'node:assert/strict'
import {
  appendStoryboard,
  appendRunStoryboard,
} from '../apps/web/canvas/flows/storyboard-timeline.js'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'
import { newShot } from '../apps/web/canvas/state/storyboard.js'
import { createNode } from '../apps/web/canvas/state/node-model.js'

function fixture() {
  const video = createNode('video', { x: 0, y: 0 }, '视频')
  video.src = '/assets/video.mp4'
  const shot = { ...newShot(), nodeId: video.id, durationFrames: 60 }
  return createDocStore({
    id: 'doc',
    name: 'doc',
    objects: { [video.id]: video },
    order: [video.id],
    storyboard: { version: 1, outline: '', shots: [shot] },
  })
}

test('分镜装配保留计划时长，追加且可一次撤销', async () => {
  const store = fixture()
  await appendStoryboard(store, async () => 90)
  assert.equal(store.doc.timeline!.clips[0].outFrame, 60)
  assert.equal(store.doc.timeline!.clips[0].source.durationFrames, 90)
  await appendStoryboard(store, async () => 90)
  assert.equal(store.doc.timeline!.clips.length, 2)
  store.undo()
  assert.equal(store.doc.timeline!.clips.length, 1)
  store.undo()
  assert.equal(store.doc.timeline, undefined)
})

test('短视频或异步期间修改分镜时不写入部分剪辑', async () => {
  const store = fixture()
  await assert.rejects(
    appendStoryboard(store, async () => 30),
    /视频不足/,
  )
  assert.equal(store.doc.timeline, undefined)
  await assert.rejects(
    appendStoryboard(store, async () => {
      store.doc.storyboard!.shots[0].title = '已修改'
      return 90
    }),
    /已变化/,
  )
  assert.equal(store.doc.timeline, undefined)
})

test('按执行轮次装配：使用本轮镜头顺序并写入执行标识，同一轮只追加一次', async () => {
  const a = createNode('video', { x: 0, y: 0 }, '一')
  const b = createNode('video', { x: 480, y: 0 }, '二')
  a.src = '/assets/a.mp4'
  b.src = '/assets/b.mp4'
  const shotA = {
    ...newShot(),
    title: '开场',
    dialogue: '第一句',
    nodeId: a.id,
    durationFrames: 60,
  }
  const shotB = {
    ...newShot(),
    title: '结尾',
    dialogue: '第二句',
    nodeId: b.id,
    durationFrames: 45,
  }
  const store = createDocStore({
    id: 'doc',
    name: 'doc',
    objects: { [a.id]: a, [b.id]: b },
    order: [a.id, b.id],
    storyboard: { version: 1, outline: '', shots: [shotA, shotB] },
  })
  // 本轮执行顺序与画布顺序相反：装配必须按执行顺序
  await appendRunStoryboard(store, 'run-1', [shotB.id, shotA.id], async () => 90)
  const clips = store.doc.timeline!.clips
  assert.deepEqual(
    clips.map((clip) => clip.name),
    ['结尾', '开场'],
  )
  assert.ok(clips.every((clip) => clip.source.runId === 'run-1'))
  assert.equal(clips[0].subtitle, '第二句')
  await assert.rejects(
    appendRunStoryboard(store, 'run-1', [shotB.id, shotA.id], async () => 90),
    /已装配过/,
  )
  // 第二轮可以继续追加，整批一次撤销
  await appendRunStoryboard(store, 'run-2', [shotA.id], async () => 90)
  assert.equal(store.doc.timeline!.clips.length, 3)
  store.undo()
  assert.equal(store.doc.timeline!.clips.length, 2)
})

test('执行轮次装配校验素材缺失与视频时长不足', async () => {
  const a = createNode('video', { x: 0, y: 0 }, '未生成')
  const shotA = { ...newShot(), nodeId: a.id, durationFrames: 60 }
  const store = createDocStore({
    id: 'doc-missing',
    name: 'doc',
    objects: { [a.id]: a },
    order: [a.id],
    storyboard: { version: 1, outline: '', shots: [shotA] },
  })
  await assert.rejects(
    appendRunStoryboard(store, 'run-x', [shotA.id], async () => 90),
    /缺少已生成素材/,
  )
  store.mutateTransient((document) => {
    document.objects[a.id].kind = 'video'
    document.objects[a.id].src = '/assets/short.mp4'
  })
  await assert.rejects(
    appendRunStoryboard(store, 'run-x', [shotA.id], async () => 30),
    /视频不足/,
  )
  assert.equal(store.doc.timeline, undefined)
})
