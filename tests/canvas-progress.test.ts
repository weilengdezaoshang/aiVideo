import test from 'node:test'
import assert from 'node:assert/strict'
import { createNode } from '../apps/web/canvas/state/node-model.js'
import { nodeProgressLabel } from '../apps/web/canvas/engine/node-appearance.js'

test('node progress follows reported values and never claims completion before output', () => {
  const obj = createNode('image', { x: 0, y: 0 }, '图片')
  assert.equal(nodeProgressLabel(obj), '')
  obj.nodeRun = { requestId: 'test', snapshot: obj.nodeDraft!, status: 'running', progress: 0.75 }
  assert.equal(nodeProgressLabel(obj), '任务进度 75%')
  obj.nodeRun.progress = 1
  assert.equal(nodeProgressLabel(obj), '任务进度 99%')
  obj.nodeRun.progress = Number.NaN
  assert.equal(nodeProgressLabel(obj), '生成中…')
  obj.nodeRun.status = 'queued'
  assert.equal(nodeProgressLabel(obj), '排队中')
  obj.nodeRun.status = 'failed'
  assert.equal(nodeProgressLabel(obj), '生成失败 · 点击重试')
})
