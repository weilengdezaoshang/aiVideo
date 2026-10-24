import test from 'node:test'
import assert from 'node:assert/strict'
import {
  buildRequestContext,
  captureRequestContext,
  inferBatchIntent,
} from '../apps/web/canvas/agent/request-context.js'
import { AgentRequestSchema } from '../apps/web/shared/agent-protocol.js'
const id = '11111111-1111-4111-8111-111111111111'
test('请求冻结选区与视口，保存等待中的变化不影响本轮；只发资源ID', () => {
  const source = {
    selection: ['cat'],
    highlights: ['cat'],
    viewport: { bbox: [0, 0, 1280, 720] as [number, number, number, number], zoom: 1 },
    clientId: 'client',
    tabId: 'tab',
  }
  const snapshot = captureRequestContext(source)
  source.selection[0] = 'video'
  source.viewport.bbox[0] = 50
  const request = buildRequestContext({
    requestId: id,
    conversationId: id,
    documentId: id,
    revision: 7,
    message: '改成水彩',
    snapshot,
  })
  assert.deepEqual(request.selection.ids, ['cat'])
  assert.equal(request.viewport.bbox[0], 0)
  assert.equal(request.doc_version, 7)
  assert.equal(request.request_id, id)
  assert.equal('label' in request, false)
  assert.equal('history' in request, false)
})
test('高亮与选区不一致拒绝发送，伪造身份拒绝，节点描述剥离', () => {
  assert.throws(
    () =>
      captureRequestContext({
        selection: ['cat'],
        highlights: ['video'],
        viewport: { bbox: [0, 0, 1, 1], zoom: 1 },
        clientId: 'client',
        tabId: 'tab',
      }),
    /SELECTION_MISMATCH/,
  )
  const request = buildRequestContext({
    requestId: id,
    conversationId: id,
    documentId: id,
    revision: 7,
    message: '改成水彩',
    snapshot: {
      selection: [],
      highlights: [],
      viewport: { bbox: [0, 0, 1, 1], zoom: 1 },
      clientId: 'client',
      tabId: 'tab',
    },
  })
  assert.equal(AgentRequestSchema.safeParse({ ...request, role: 'admin' }).success, false)
  const parsed = AgentRequestSchema.parse({
    ...request,
    message_text: undefined,
    message: [
      { type: 'mention', id: 'cat', label: 'forged', kind: 'video' },
      { type: 'text', text: '改成水彩' },
    ],
    mentions: [{ id: 'cat', label: 'forged' }],
  })
  assert.deepEqual(parsed.mentions, [{ id: 'cat' }])
  assert.deepEqual(parsed.message?.[0], { type: 'mention', id: 'cat' })
})

test('批量意图只在按钮或明确批量用语加多选时成立', () => {
  assert.equal(inferBatchIntent('全部抠图换白底', 100), true)
  assert.equal(inferBatchIntent('不要批量全部处理', 100), false)
  assert.equal(inferBatchIntent('比较这两张哪个更好', 2), false)
  assert.equal(inferBatchIntent('抠图', 100), false)
  assert.equal(inferBatchIntent('抠图', 1, 'batch'), true)
  assert.equal(inferBatchIntent('全部', 1), false)
})

test('仅参考节点内容身份变化拒发；换选区和其他节点变化可发送', async () => {
  const { assertRequestNodesUnchanged } =
    await import('../apps/web/canvas/agent/request-context.js')
  const snapshot = {
    selection: ['cat'],
    highlights: ['cat'],
    nodeFingerprints: { cat: 'asset:1' },
    viewport: { bbox: [0, 0, 1, 1] as [number, number, number, number], zoom: 1 },
    clientId: 'client',
    tabId: 'tab',
  }
  assert.doesNotThrow(() =>
    assertRequestNodesUnchanged(snapshot, {
      ...snapshot,
      selection: ['dog'],
      highlights: ['dog'],
      nodeFingerprints: { cat: 'asset:1', dog: 'other:2' },
    }),
  )
  assert.throws(
    () =>
      assertRequestNodesUnchanged(snapshot, {
        ...snapshot,
        nodeFingerprints: { cat: 'asset:2' },
      }),
    /REFERENCE_CHANGED/,
  )
  assert.throws(
    () => assertRequestNodesUnchanged(snapshot, { ...snapshot, nodeFingerprints: {} }),
    /REFERENCE_CHANGED/,
  )
})
