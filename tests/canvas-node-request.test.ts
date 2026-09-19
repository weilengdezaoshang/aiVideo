import test from 'node:test'
import assert from 'node:assert/strict'
import { createNode, type Capability } from '../apps/web/canvas/state/node-model.js'
import { prepareNodeDraft, nodeRequest } from '../apps/web/canvas/flows/node-request.js'

const cap: Capability = {
  supported: true,
  models: [{ id: 'model', name: '模型' }],
  ratios: ['16:9'],
  resolutions: [512],
  referenceLimit: 1,
  durations: [4, 8],
}

test('准备参数不修改节点，构造请求保留模型引用和幂等标识', () => {
  const node = createNode('image', { x: 0, y: 0 }, '镜头')
  node.nodeDraft!.prompt = '产品特写'
  node.nodeDraft!.references = [
    { assetId: 'asset', ext: 'png', name: '产品', sourceNodeId: 'source' },
  ]
  const before = structuredClone(node)
  const draft = prepareNodeDraft(node, cap)
  assert.deepEqual(node, before)
  assert.equal(draft.model, 'model')
  assert.equal(draft.ratio, '16:9')
  const raw = nodeRequest('document', node, 'request', draft)
  assert.equal(raw.documentId, 'document')
  assert.equal(raw.requestId, 'request')
  assert.equal(raw.clientRef, node.id)
  assert.equal(raw.width, 512)
  assert.equal(raw.height, 288)
  assert.equal(raw.batchCount, 1)
  assert.deepEqual(raw.referenceAssetIds, ['asset'])
  draft.references[0].name = '外部修改'
  assert.equal(node.nodeDraft!.references[0].name, '产品')
})

test('准备拒绝不支持的视频时长、超限引用及已有输出，不静默修改时长', () => {
  const node = createNode('video', { x: 0, y: 0 }, '视频')
  node.nodeDraft!.prompt = '产品旋转'
  node.nodeDraft!.durationSec = 5
  assert.throws(() => prepareNodeDraft(node, cap), /视频时长/)
  assert.equal(node.nodeDraft!.durationSec, 5)
  node.nodeDraft!.durationSec = 4
  node.nodeDraft!.references = Array.from({ length: 2 }, () => ({
    assetId: 'a',
    ext: 'png',
    name: '图',
  }))
  assert.throws(() => prepareNodeDraft(node, cap), /参考图数量/)
  node.src = '/result.mp4'
  assert.throws(() => prepareNodeDraft(node, cap), /新的生成节点/)
})
