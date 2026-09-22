import test from 'node:test'
import assert from 'node:assert/strict'
import { documentConnections, connectionPoints } from '../apps/web/canvas/state/connections.js'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'
import { cmdRemoveObjects, cmdUpdateObject } from '../apps/web/canvas/state/commands.js'
import { createNode } from '../apps/web/canvas/state/node-model.js'

test('引用连线随引用删除、撤销和文档序列化恢复；删除源节点不会产生悬空线', () => {
  const source = createNode('image', { x: 0, y: 0 }, '角色')
  source.assetId = 'asset-1'
  const target = createNode('video', { x: 500, y: 0 }, '镜头')
  target.nodeDraft!.references = [
    { assetId: 'asset-1', ext: 'png', name: '角色', sourceNodeId: source.id },
  ]
  const store = createDocStore({
    id: 'doc',
    name: '测试',
    objects: { [source.id]: source, [target.id]: target },
    order: [source.id, target.id],
  })
  assert.equal(documentConnections(store.doc.objects).length, 1)
  store.apply(
    cmdUpdateObject(
      target.id,
      { nodeDraft: { ...target.nodeDraft, references: [] } },
      { nodeDraft: target.nodeDraft },
    ),
  )
  assert.deepEqual(documentConnections(store.doc.objects), [])
  store.undo()
  assert.equal(documentConnections(JSON.parse(JSON.stringify(store.doc)).objects).length, 1)
  store.apply(cmdRemoveObjects([{ object: source, orderIndex: 0 }]))
  assert.deepEqual(documentConnections(store.doc.objects), [])
  store.undo()
  assert.equal(documentConnections(store.doc.objects).length, 1)
})

test('已生成结果显示实际多参考谱系，不把后来修改的草稿误当生成来源', () => {
  const source = createNode('image', { x: 0, y: 0 }, '角色')
  const other = createNode('image', { x: 0, y: 400 }, '场景')
  const target = createNode('image', { x: 500, y: 0 }, '结果')
  target.src = '/output.png'
  target.lineage = {
    fromId: source.id,
    params: { references: [{ sourceNodeId: source.id }, { sourceNodeId: other.id }] },
  }
  target.nodeDraft!.references = [
    { assetId: 'self', ext: 'png', name: '', sourceNodeId: target.id },
  ]
  const edges = documentConnections({ [source.id]: source, [other.id]: other, [target.id]: target })
  assert.deepEqual(
    edges.map((e) => e.sourceId),
    [source.id, other.id],
  )
  assert.ok(edges.every((e) => e.kind === 'lineage'))
})

test('旋转后的端口与对象几何一致，向左连接仍有有效曲线', () => {
  const points = connectionPoints(
    { x: 0, y: 0, width: 100, height: 80, rotation: 90 },
    { x: -200, y: 100, width: 80, height: 80 },
  )
  assert.ok(Math.abs(points[0] + 40) < 0.001)
  assert.equal(points[1], 100)
  assert.deepEqual(points.slice(-2), [-200, 140])
  assert.ok(points.every(Number.isFinite))
})
