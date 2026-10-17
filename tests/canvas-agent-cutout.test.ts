import assert from 'node:assert/strict'
import test from 'node:test'
import { openAgentCutout } from '../apps/web/canvas/flows/agent-cutout.js'
import { createCanvasView } from '../apps/web/canvas/core/canvas-view.js'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'
import type { AgentRun } from '../apps/web/canvas/agent/types.js'

const run: AgentRun = {
  id: 'run',
  clientRequestId: 'request',
  status: 'waiting_approval',
  message: '抠图',
  reply: null,
  error: null,
  modelId: 'host',
  baseRevision: 1,
  modelCalls: [],
  toolCalls: [],
  createdAt: '',
  updatedAt: '',
  canvasAction: {
    type: 'cutout',
    documentId: 'doc',
    sourceObjectId: 'image',
    sourceAssetId: 'asset',
  },
}
function setup() {
  const store = createDocStore({
    id: 'doc',
    name: 'test',
    objects: {
      image: { id: 'image', kind: 'image', x: 0, y: 0, width: 100, height: 100, assetId: 'asset' },
      other: {
        id: 'other',
        kind: 'image',
        x: 200,
        y: 0,
        width: 100,
        height: 100,
        assetId: 'other',
      },
    },
    order: ['image', 'other'],
  })
  const view = createCanvasView({ store })
  return { store, view }
}

test('迟到的抠图响应不抢走新选区，显式点击后才定位原图', () => {
  const { view } = setup()
  let opened = 0
  view.select(['other'])
  const input = {
    run,
    view,
    applying: false,
    start: () => {
      opened++
    },
  }
  assert.throws(() => openAgentCutout({ ...input, automatic: true }), /选区已变化/)
  assert.deepEqual(view.getSnapshot().selection, ['other'])
  assert.equal(opened, 0)
  openAgentCutout({ ...input, automatic: false })
  assert.equal(opened, 1)
  assert.deepEqual(view.getSnapshot().selection, ['image'])
  view.destroy()
})

test('已进入同一任务不重复识别，换图或合成中不能错误进入', () => {
  const { store, view } = setup()
  view.select(['image'])
  view.activateTool('mask')
  let opened = 0
  const input = {
    run,
    view,
    applying: false,
    automatic: true,
    start: () => {
      opened++
    },
  }
  openAgentCutout({ ...input, activeRunId: run.id })
  assert.equal(opened, 0)
  assert.throws(() => openAgentCutout({ ...input, applying: true }), /正在提交/)
  store.doc.objects.image = { ...store.doc.objects.image, assetId: 'replaced' }
  assert.throws(() => openAgentCutout(input), /源图已变化/)
  assert.equal(opened, 0)
  view.destroy()
})
