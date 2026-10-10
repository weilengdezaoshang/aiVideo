import assert from 'node:assert/strict'
import test from 'node:test'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'
import {
  confirmPendingGroup,
  cancelPendingGroup,
  submitGroupGeneration,
} from '../apps/web/canvas/flows/group-generate.js'

test('组生成携带文档归属和稳定请求标识，断线查询404不会删除未知结果占位', async () => {
  const store = createDocStore({ id: 'doc', name: '组生成', objects: {}, order: [] })
  let submitted: { requestId: string; documentId: string; clientRefs: string[] } | undefined
  let saved = false
  const result = await submitGroupGeneration({
    docStore: store,
    genParams: { prompt: '森林', model: 'mock-test' },
    ensureSaved: async () => {
      saved = true
    },
    fetchImpl: (async (_url, init) => {
      assert.equal(saved, true)
      submitted = JSON.parse(String(init?.body))
      throw new Error('connection lost')
    }) as typeof fetch,
  })
  assert.equal(result.ok, false)
  assert.equal(submitted?.documentId, 'doc')
  assert.equal(submitted?.clientRefs.length, 4)
  const group = Object.values(store.doc.groups ?? {})[0]
  assert.equal(group.requestId, submitted?.requestId)
  const before = structuredClone(store.doc.objects)
  const recovery = await confirmPendingGroup({
    docStore: store,
    groupId: Object.keys(store.doc.groups!)[0],
    fetchImpl: (async () => new Response('{}', { status: 404 })) as typeof fetch,
  })
  assert.equal(recovery.found, false)
  assert.deepEqual(store.doc.objects, before)
  const groupId = Object.keys(store.doc.groups!)[0]
  await assert.rejects(
    cancelPendingGroup({
      docStore: store,
      groupId,
      fetchImpl: (async () => new Response('{}', { status: 503 })) as typeof fetch,
    }),
  )
  assert.deepEqual(store.doc.objects, before)
  await cancelPendingGroup({
    docStore: store,
    groupId,
    fetchImpl: (async (_url, init) => {
      assert.equal(init?.method, 'DELETE')
      return new Response('{}')
    }) as typeof fetch,
  })
  assert.equal(store.doc.order.length, 0)
})
