import './dom.js'
import test from 'node:test'
import assert from 'node:assert/strict'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'
import { createGenerationCoordinator } from '../apps/web/canvas/state/generation-coordinator.js'
import { loadLocalDraft } from '../apps/web/canvas/state/document-api.js'
import {
  connectGenerationEvents,
  type GenerationEventSource,
} from '../apps/web/canvas/state/generation-events.js'
import { selectCanvasObject } from '../apps/web/canvas/state/generation-entity.js'
import type { JobLike } from '../apps/web/canvas/state/generation-reducer.js'

const storeFor = () =>
  createDocStore({ id: crypto.randomUUID(), name: '恢复测试', revision: 1, objects: {}, order: [] })
const params = { prompt: 'cat', width: 1024, height: 576 }

test('丢失受理响应：持久化 requestId/documentId 后提交，按原请求找回且只 POST 一次', async () => {
  const store = storeFor()
  let posts = 0
  let saved = false
  let job: JobLike | undefined
  const coordinator = createGenerationCoordinator({
    docStore: store,
    ensureSaved: async () => {
      saved = true
    },
    fetchImpl: async (input, init) => {
      if (String(input) === '/api/generate') {
        posts++
        const body = JSON.parse(String(init?.body)) as {
          requestId: string
          documentId: string
          clientRef: string
        }
        assert.equal(saved, true)
        assert.equal(body.documentId, store.doc.id)
        assert.match(body.requestId, /^[0-9a-f-]{36}$/)
        assert.ok(
          Object.values(loadLocalDraft(store.doc.id)?.generations || {}).some(
            (g) => g.operationId === body.requestId,
          ),
        )
        job = {
          id: 'accepted',
          requestId: body.requestId,
          documentId: body.documentId,
          clientRef: body.clientRef,
          status: 'running',
          stateVersion: 2,
        }
        throw new Error('response lost')
      }
      assert.equal(String(input), `/api/generation-requests/${job?.requestId}`)
      return Response.json({ job })
    },
  })
  const result = await coordinator.submit({ genParams: params })
  assert.equal(result.ok, true)
  assert.equal(result.jobId, 'accepted')
  assert.equal(posts, 1)
  assert.equal(store.doc.generations?.[result.generationId].status, 'running')
})

test('未知提交经刷新仍阻止重复 POST，取消确认前不释放请求', async () => {
  const store = storeFor()
  let posts = 0
  let canCancel = false
  const deleted: string[] = []
  const fetchImpl: typeof fetch = async (input, init) => {
    if (String(input) === '/api/generate') {
      posts++
      throw new Error('timeout')
    }
    if (init?.method === 'DELETE') {
      deleted.push(String(input))
      return Response.json({}, { status: canCancel ? 200 : 503 })
    }
    return Response.json({}, { status: 404 })
  }
  const coordinator = createGenerationCoordinator({ docStore: store, fetchImpl })
  const first = await coordinator.submit({ genParams: params })
  assert.equal(first.ok, false)
  assert.equal(store.doc.generations?.[first.generationId].status, 'uncertain')
  const again = await coordinator.submit({ genParams: params })
  assert.equal(again.operationId, first.operationId)
  assert.equal(store.doc.order.length, 1)
  assert.equal(posts, 1)
  const reopened = createDocStore(JSON.parse(JSON.stringify(store.doc)) as typeof store.doc)
  const recovered = createGenerationCoordinator({ docStore: reopened, fetchImpl })
  assert.equal((await recovered.submit({ genParams: params })).operationId, first.operationId)
  await assert.rejects(recovered.cancel(first.generationId), /取消未确认/)
  assert.equal((await recovered.submit({ genParams: params })).operationId, first.operationId)
  assert.equal(posts, 1)
  canCancel = true
  await recovered.cancel(first.generationId)
  assert.equal(deleted.at(-1), `/api/generation-requests/${first.operationId}`)
  assert.equal(reopened.doc.generations?.[first.generationId].status, 'cancelled')
  const next = await recovered.submit({ genParams: params })
  assert.notEqual(next.operationId, first.operationId)
  assert.equal(posts, 2)
})

test('生成意图保存失败时不发送付费生成请求', async () => {
  let calls = 0
  const coordinator = createGenerationCoordinator({
    docStore: storeFor(),
    ensureSaved: async () => {
      throw new Error('保存冲突')
    },
    fetchImpl: async () => {
      calls++
      return Response.json({})
    },
  })
  const result = await coordinator.submit({ genParams: params })
  assert.equal(result.ok, false)
  assert.match(result.error || '', /保存冲突/)
  assert.equal(calls, 0)
})

test('近期列表没有旧任务时按 ID 找回；迟到的旧版本不覆盖完成结果', async () => {
  const store = storeFor()
  const accepted = createGenerationCoordinator({
    docStore: store,
    fetchImpl: async () => Response.json({ jobId: 'old-job' }),
  })
  const result = await accepted.submit({ genParams: params })
  const reloaded = createDocStore(JSON.parse(JSON.stringify(store.doc)) as typeof store.doc)
  const coordinator = createGenerationCoordinator({ docStore: reloaded })
  const paths: string[] = []
  const events = connectGenerationEvents({
    docStore: reloaded,
    coordinator,
    eventSourceFactory: () => ({ addEventListener() {}, close() {} }),
    fetchImpl: async (input) => {
      paths.push(String(input))
      if (String(input).includes('?')) {
        return Response.json({ jobs: [] })
      }
      return Response.json({
        job: {
          id: 'old-job',
          clientRef: result.objectId,
          status: 'completed',
          stateVersion: 9,
          images: [{ id: 'asset', url: '/asset.png', params: { width: 1024, height: 576 } }],
        },
      })
    },
  })
  try {
    await events.reconcileNow()
    assert.deepEqual(paths, ['/api/jobs?all=1&limit=100', '/api/jobs/old-job'])
    assert.equal(selectCanvasObject(reloaded.doc, result.objectId)?.src, '/asset.png')
    coordinator.acceptJob({
      id: 'old-job',
      clientRef: result.objectId,
      status: 'queued',
      stateVersion: 1,
    })
    assert.equal(selectCanvasObject(reloaded.doc, result.objectId)?.kind, 'image')
  } finally {
    events.close()
  }
})

test('SSE 活跃快照和失败的单任务查询不能把遗漏任务误判丢失', async () => {
  const doc = {
    id: 'legacy',
    name: 'old',
    order: ['p'],
    objects: {
      p: {
        id: 'p',
        kind: 'placeholder' as const,
        x: 0,
        y: 0,
        width: 100,
        height: 100,
        gen: { jobId: 'outside-list' },
      },
    },
  }
  const listeners = new Map<string, (event: { data: string }) => void>()
  const es: GenerationEventSource = {
    addEventListener: (name, listener) => {
      listeners.set(name, listener)
    },
    close() {},
  }
  const events = connectGenerationEvents({
    docStore: { mutateTransient: (fn) => fn(doc) },
    eventSourceFactory: () => es,
    fetchImpl: async (input) =>
      String(input).includes('?')
        ? Response.json({ jobs: [] })
        : Response.json({}, { status: 503 }),
  })
  try {
    await events.reconcileNow()
    listeners.get('snapshot')?.({ data: JSON.stringify({ jobs: [] }) })
    assert.equal(doc.objects.p.kind, 'placeholder')
    assert.equal(doc.objects.p.gen.jobId, 'outside-list')
  } finally {
    events.close()
  }
})

test('较晚的受理响应不把先到的运行事件回退为排队', async () => {
  const store = storeFor()
  let release!: (response: Response) => void
  const coordinator = createGenerationCoordinator({
    docStore: store,
    fetchImpl: async () =>
      new Promise<Response>((resolve) => {
        release = resolve
      }),
  })
  const submitting = coordinator.submit({ genParams: params })
  const entry = coordinator.runtime.values()[0]
  coordinator.acceptJob({
    id: 'fast-job',
    clientRef: entry.objectId,
    requestId: entry.operationId,
    status: 'running',
    progress: 0.5,
    stateVersion: 2,
  })
  release(Response.json({ jobId: 'fast-job' }))
  await submitting
  assert.equal(coordinator.runtime.get(entry.operationId)?.status, 'running')
  assert.equal(coordinator.runtime.get(entry.operationId)?.progress, 0.5)
  assert.equal(store.doc.generations?.[entry.generationId].status, 'running')
})

test('旧 gen 文档迁入实体后，对账不再经旧 reducer 覆盖自定义画布尺寸', async () => {
  const store = createDocStore({
    id: 'old-layout',
    name: '',
    order: ['p'],
    objects: {
      p: {
        id: 'p',
        kind: 'placeholder',
        x: 40,
        y: 70,
        width: 333,
        height: 222,
        gen: { jobId: 'legacy-job' },
      },
    },
  })
  const coordinator = createGenerationCoordinator({ docStore: store })
  const events = connectGenerationEvents({
    docStore: store,
    coordinator,
    eventSourceFactory: () => ({ addEventListener() {}, close() {} }),
    fetchImpl: async () =>
      Response.json({
        jobs: [
          {
            id: 'legacy-job',
            requestId: 'original-server-request',
            status: 'completed',
            images: [
              { id: 'legacy-result', url: '/legacy.png', params: { width: 1024, height: 576 } },
            ],
          },
        ],
      }),
  })
  try {
    await events.reconcileNow()
    const view = selectCanvasObject(store.doc, 'p')!
    assert.equal(view.src, '/legacy.png')
    assert.equal(view.width, 333)
    assert.equal(view.height, 222)
    assert.equal(view.x, 40)
  } finally {
    events.close()
  }
})
