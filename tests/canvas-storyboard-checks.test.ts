import test from 'node:test'
import assert from 'node:assert/strict'
import { runStoryboardChecks } from '../apps/web/canvas/flows/storyboard-checks.js'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'
import { createNode } from '../apps/web/canvas/state/node-model.js'
import { newShot } from '../apps/web/canvas/state/storyboard.js'

function fixture() {
  const done = createNode('image', { x: 0, y: 0 }, '已完成')
  done.src = '/images/done.png'
  const shot = { ...newShot(), title: '开场', visual: '产品特写', nodeId: done.id }
  const store = createDocStore({
    id: 'checks',
    name: 'doc',
    objects: { [done.id]: done },
    order: [done.id],
    storyboard: { version: 1, outline: '', shots: [shot] },
  })
  return { store, done, shot }
}

test('健康分镜与已装配时间线不报问题', () => {
  const { store } = fixture()
  const board = store.doc.storyboard
  const timeline = {
    version: 1 as const,
    fps: 30 as const,
    width: 1920,
    height: 1080,
    clips: [
      {
        id: 'clip-1',
        name: '开场',
        subtitle: '好状态从每天开始',
        source: {
          nodeId: board!.shots[0].nodeId!,
          kind: 'image' as const,
          url: '/images/done.png',
          durationFrames: 3000,
        },
        inFrame: 0,
        outFrame: 150,
      },
    ],
  }
  assert.deepEqual(runStoryboardChecks(board, store.doc.objects, timeline), [])
})

test('逐镜头检查未关联、已删除、生成中、失败与空画面', () => {
  const { store } = fixture()
  const pending = createNode('image', { x: 400, y: 0 }, '草稿')
  const running = createNode('image', { x: 800, y: 0 }, '生成中')
  const failed = createNode('image', { x: 1200, y: 0 }, '失败')
  store.apply({
    type: 'addObjects',
    objects: [pending, running, failed],
  })
  // addObjects 会写入浅克隆：任务状态必须设置在文档内的副本上
  const storedRunning = store.doc.objects[running.id]
  const storedFailed = store.doc.objects[failed.id]
  store.mutateTransient(() => {
    storedRunning.nodeRun = {
      requestId: 'r-running',
      status: 'running',
      snapshot: structuredClone(storedRunning.nodeDraft!),
      message: '生成中',
    }
    storedFailed.nodeRun = {
      requestId: 'r-failed',
      status: 'failed',
      snapshot: structuredClone(storedFailed.nodeDraft!),
      message: '模型超时',
    }
  })
  const board = store.doc.storyboard!
  board.shots.push(
    { ...newShot(), title: '二', visual: '', nodeId: null },
    { ...newShot(), title: '三', visual: '场景', nodeId: 'missing-node' },
    { ...newShot(), title: '四', visual: '场景', nodeId: pending.id },
    { ...newShot(), title: '五', visual: '场景', nodeId: running.id },
    { ...newShot(), title: '六', visual: '场景', nodeId: failed.id },
  )
  // 时间线只覆盖了其他节点：已生成的镜头 1 应提示未进入时间线
  const partialTimeline = {
    version: 1 as const,
    fps: 30 as const,
    width: 1920,
    height: 1080,
    clips: [
      {
        id: 'clip-partial',
        name: '二',
        source: {
          nodeId: pending.id,
          kind: 'image' as const,
          url: '/images/pending.png',
          durationFrames: 3000,
        },
        inFrame: 0,
        outFrame: 150,
      },
    ],
  }
  const issues = runStoryboardChecks(board, store.doc.objects, partialTimeline)
  const messages = issues.map((issue) => issue.message)
  assert.ok(messages.some((m) => m.includes('镜头 1') && m.includes('尚未进入时间线')))
  assert.ok(messages.some((m) => m.includes('镜头 2') && m.includes('尚未关联生成节点')))
  assert.ok(messages.some((m) => m.includes('镜头 2') && m.includes('画面描述为空')))
  assert.ok(messages.some((m) => m.includes('镜头 3') && m.includes('关联节点已删除')))
  assert.ok(messages.some((m) => m.includes('镜头 4') && m.includes('尚未生成素材')))
  assert.ok(messages.some((m) => m.includes('镜头 5') && m.includes('正在生成中')))
  assert.ok(messages.some((m) => m.includes('镜头 6') && m.includes('生成失败：模型超时')))
  assert.ok(issues.some((issue) => issue.severity === 'error' && issue.message.includes('镜头 6')))
})

test('时间线检查外部素材地址、过短字幕与未装配镜头', () => {
  const { store, done } = fixture()
  const timeline = {
    version: 1 as const,
    fps: 30 as const,
    width: 1920,
    height: 1080,
    clips: [
      {
        id: 'clip-ext',
        name: '外部',
        subtitle: '',
        source: {
          nodeId: done.id,
          kind: 'image' as const,
          url: 'https://example.com/a.png',
          durationFrames: 3000,
        },
        inFrame: 0,
        outFrame: 150,
      },
    ],
  }
  const issues = runStoryboardChecks(store.doc.storyboard, store.doc.objects, timeline)
  assert.ok(issues.some((i) => i.severity === 'error' && i.message.includes('外部地址')))
  // 台词片段过短：时长 30 帧（1 秒）带台词
  const shortTimeline = {
    ...timeline,
    clips: [
      {
        id: 'clip-short',
        name: '过短',
        subtitle: '太快了',
        source: { ...timeline.clips[0].source, url: '/images/done.png' },
        inFrame: 0,
        outFrame: 30,
      },
    ],
  }
  const short = runStoryboardChecks(store.doc.storyboard, store.doc.objects, shortTimeline)
  assert.ok(short.some((i) => i.severity === 'warning' && i.message.includes('字幕可能一闪而过')))
  // 外部素材 + 已覆盖镜头 → 不再报“未进入时间线”
  assert.ok(!short.some((i) => i.message.includes('尚未进入时间线')))
})

test('空分镜给出明确提示', () => {
  const issues = runStoryboardChecks(undefined, {}, undefined)
  assert.deepEqual(issues, [{ severity: 'warning', message: '尚未创建分镜镜头' }])
})
