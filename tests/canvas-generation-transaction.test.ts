import test from 'node:test'
import assert from 'node:assert/strict'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'
import {
  cmdAddObjects,
  cmdMoveObjects,
  cmdRemoveObjects,
} from '../apps/web/canvas/state/commands.js'
import {
  getGenerationCoordinator,
  retryError,
  submitGeneration,
} from '../apps/web/canvas/flows/generate.js'
import { selectCanvasObject } from '../apps/web/canvas/state/generation-entity.js'

function responseFetch(calls: string[]) {
  return (async (url: string) => {
    calls.push(url)
    if (url.startsWith('/api/jobs/')) {
      return Response.json({ ok: true })
    }
    return Response.json({ jobId: 'job-1' }, { status: 202 })
  }) as typeof fetch
}

test('生成完成后 Undo/Redo 恢复同一产物且不重新生成', async () => {
  const store = createDocStore({ id: 'doc', name: 'doc', objects: {}, order: [] })
  const calls: string[] = []
  const fetchImpl = responseFetch(calls)
  const result = await submitGeneration({
    docStore: store,
    genParams: { prompt: '猫', width: 512, height: 512 },
    fetchImpl,
  })
  assert.equal(result.ok, true)
  if (!result.ok) {
    return
  }

  const coordinator = getGenerationCoordinator(store)
  assert.equal(
    coordinator.acceptJob({
      id: 'job-1',
      clientRef: result.objId,
      status: 'completed',
      images: [{ id: 'image-1', url: '/images/result.png', file: 'result.png' }],
    }),
    true,
  )
  assert.equal(selectCanvasObject(store.doc, result.objId)?.imageId, 'image-1')

  assert.equal(store.undo(), true)
  assert.equal(store.doc.objects[result.objId], undefined)
  assert.equal(store.redo(), true)
  assert.equal(selectCanvasObject(store.doc, result.objId)?.imageId, 'image-1')
  assert.deepEqual(
    calls.filter((url) => url === '/api/generate'),
    ['/api/generate'],
  )
})

test('Undo 后迟到完成不复活节点，Redo 恢复迟到产物与原位置', async () => {
  const store = createDocStore({ id: 'doc', name: 'doc', objects: {}, order: [] })
  const fetchImpl = responseFetch([])
  const result = await submitGeneration({
    docStore: store,
    genParams: { prompt: '海', width: 640, height: 480 },
    anchor: { x: 80, y: 120 },
    fetchImpl,
  })
  assert.equal(result.ok, true)
  if (!result.ok) {
    return
  }
  assert.equal(store.undo(), true)

  getGenerationCoordinator(store).acceptJob({
    id: 'job-1',
    clientRef: result.objId,
    status: 'completed',
    images: [{ id: 'late-image', url: '/images/late.png', file: 'late.png' }],
  })
  assert.equal(store.doc.objects[result.objId], undefined, '迟到结果不能重新插入画布')

  assert.equal(store.redo(), true)
  const restored = selectCanvasObject(store.doc, result.objId)!
  assert.equal(restored.imageId, 'late-image')
  assert.deepEqual({ x: restored.x, y: restored.y }, { x: 80, y: 120 })
})

test('生成结果上的后续编辑保持独立的 Undo/Redo 顺序', async () => {
  const store = createDocStore({ id: 'doc', name: 'doc', objects: {}, order: [] })
  const result = await submitGeneration({
    docStore: store,
    genParams: { prompt: '山' },
    fetchImpl: responseFetch([]),
  })
  assert.equal(result.ok, true)
  if (!result.ok) {
    return
  }
  getGenerationCoordinator(store).acceptJob({
    id: 'job-1',
    clientRef: result.objId,
    status: 'completed',
    images: [{ id: 'mountain', url: '/images/mountain.png', file: 'mountain.png' }],
  })

  const before = { x: store.doc.objects[result.objId].x, y: store.doc.objects[result.objId].y }
  const after = { x: before.x + 100, y: before.y + 40 }
  store.apply(cmdMoveObjects([{ id: result.objId, from: before, to: after }]))
  store.undo()
  assert.deepEqual(
    { x: store.doc.objects[result.objId].x, y: store.doc.objects[result.objId].y },
    before,
  )
  store.undo()
  assert.equal(store.doc.objects[result.objId], undefined)
  store.redo()
  store.redo()
  assert.deepEqual(
    { x: store.doc.objects[result.objId].x, y: store.doc.objects[result.objId].y },
    after,
  )
})

test('旧 gen 字段迁入运行态且不再序列化进文档', () => {
  const store = createDocStore({
    id: 'doc',
    name: 'doc',
    order: ['legacy'],
    objects: {
      legacy: {
        id: 'legacy',
        kind: 'placeholder',
        x: 1,
        y: 2,
        width: 10,
        height: 10,
        gen: { jobId: 'old-job', progress: 0.5, message: '生成中' },
      },
    },
  })
  const coordinator = getGenerationCoordinator(store, { fetchImpl: responseFetch([]) })
  assert.equal(coordinator.runtime.getByJobId('old-job')?.jobId, 'old-job')
  assert.equal(JSON.stringify(store.doc).includes('生成中'), false)
  assert.equal(store.doc.generations?.[store.doc.objects.legacy.generationId!]?.jobId, 'old-job')
})

test('完成事件先于 POST 响应时不会被回退为排队状态', async () => {
  const store = createDocStore({ id: 'doc', name: 'doc', objects: {}, order: [] })
  let resolveResponse!: (response: Response) => void
  const fetchImpl = (() =>
    new Promise<Response>((resolve) => {
      resolveResponse = resolve
    })) as typeof fetch
  let objectId = ''
  const pending = submitGeneration({
    docStore: store,
    genParams: { prompt: '先完成' },
    fetchImpl,
    onPlaceholderCreated: (placeholder) => {
      objectId = placeholder.id
    },
  })
  const coordinator = getGenerationCoordinator(store)
  coordinator.acceptJob({
    id: 'job-fast',
    clientRef: objectId,
    status: 'completed',
    images: [{ id: 'fast-image', url: '/images/fast.png', file: 'fast.png' }],
  })
  resolveResponse(Response.json({ jobId: 'job-fast' }, { status: 202 }))
  await pending

  assert.equal(selectCanvasObject(store.doc, objectId)?.imageId, 'fast-image')
  assert.equal(coordinator.runtime.getByObjectId(objectId)?.status, 'completed')
})

test('生成完成只更新实体，不覆盖 placement 的后续布局', async () => {
  const store = createDocStore({ id: 'doc', name: 'doc', objects: {}, order: [] })
  const result = await submitGeneration({
    docStore: store,
    genParams: { prompt: '布局安全', width: 512, height: 512 },
    fetchImpl: responseFetch([]),
  })
  assert.equal(result.ok, true)
  if (!result.ok) {
    return
  }
  const placement = store.doc.objects[result.objId]
  store.apply(
    cmdMoveObjects([
      { id: placement.id, from: { x: placement.x, y: placement.y }, to: { x: 900, y: 700 } },
    ]),
  )
  getGenerationCoordinator(store).acceptJob({
    id: 'job-1',
    clientRef: result.objId,
    status: 'completed',
    images: [{ id: 'layout-safe', url: '/images/layout.png', file: 'layout.png' }],
  })

  assert.deepEqual(
    { x: store.doc.objects[result.objId].x, y: store.doc.objects[result.objId].y },
    { x: 900, y: 700 },
  )
  assert.equal(store.doc.objects[result.objId].kind, 'placeholder', '持久 placement 不物化产物')
  assert.equal(selectCanvasObject(store.doc, result.objId)?.imageId, 'layout-safe')
})

test('同一生成实体可被多个 placement 引用，删除一个不影响另一个', async () => {
  const store = createDocStore({ id: 'doc', name: 'doc', objects: {}, order: [] })
  const result = await submitGeneration({
    docStore: store,
    genParams: { prompt: '共享实体' },
    fetchImpl: responseFetch([]),
  })
  assert.equal(result.ok, true)
  if (!result.ok) {
    return
  }
  const first = store.doc.objects[result.objId]
  const second = { ...first, id: 'second-placement', x: first.x + 600 }
  store.apply(cmdAddObjects([second]))
  store.apply(cmdRemoveObjects([{ object: first, orderIndex: 0 }]))

  assert.ok(store.doc.generations?.[result.generationId])
  assert.equal(store.doc.objects[result.objId], undefined)
  assert.equal(store.doc.objects['second-placement'].generationId, result.generationId)
})

test('实体版本拒绝重复和乱序 SSE', async () => {
  const store = createDocStore({ id: 'doc', name: 'doc', objects: {}, order: [] })
  const result = await submitGeneration({
    docStore: store,
    genParams: { prompt: '版本' },
    fetchImpl: responseFetch([]),
  })
  assert.equal(result.ok, true)
  if (!result.ok) {
    return
  }
  const coordinator = getGenerationCoordinator(store)
  const current = store.doc.generations![result.generationId]
  const baseVersion = current.version
  coordinator.acceptEvent({
    generationId: result.generationId,
    operationId: result.operationId,
    jobId: 'job-1',
    status: 'completed',
    version: baseVersion + 2,
    image: { id: 'new-image', url: '/images/new.png', file: 'new.png' },
  })
  coordinator.acceptEvent({
    generationId: result.generationId,
    operationId: result.operationId,
    jobId: 'job-1',
    status: 'completed',
    version: baseVersion + 1,
    image: { id: 'old-image', url: '/images/old.png', file: 'old.png' },
  })

  assert.equal(store.doc.generations![result.generationId].result?.imageId, 'new-image')
})

test('重试复用 placement id 时，SSE 落到最新 generation', async () => {
  const store = createDocStore({ id: 'doc', name: 'doc', objects: {}, order: [] })
  let submits = 0
  const fetchImpl = (async (url: string) => {
    if (url === '/api/generate') {
      submits += 1
      return Response.json({ jobId: submits === 1 ? 'job-old' : 'job-new' }, { status: 202 })
    }
    return Response.json({ ok: true })
  }) as typeof fetch
  const first = await submitGeneration({
    docStore: store,
    genParams: { prompt: '重试' },
    fetchImpl,
  })
  assert.equal(first.ok, true)
  if (!first.ok) {
    return
  }
  const coordinator = getGenerationCoordinator(store)
  coordinator.acceptJob({ id: 'job-old', clientRef: first.objId, status: 'failed', error: '失败' })

  const retried = await retryError({ docStore: store, objId: first.objId, fetchImpl })
  assert.equal(retried.ok, true)
  if (!retried.ok) {
    return
  }
  assert.notEqual(retried.generationId, first.generationId)
  coordinator.acceptJob({
    id: 'job-new',
    clientRef: first.objId,
    status: 'completed',
    images: [{ id: 'retry-image', url: '/images/retry.png', file: 'retry.png' }],
  })

  assert.equal(store.doc.objects[first.objId].generationId, retried.generationId)
  assert.equal(store.doc.generations?.[retried.generationId].result?.imageId, 'retry-image')
  assert.equal(store.doc.generations?.[first.generationId].result, undefined)
})

test('生成实体写入与补全会发出可持久化事件', () => {
  const store = createDocStore({ id: 'doc', name: 'doc', objects: {}, order: [] })
  const events: string[] = []
  store.subscribe((event) => {
    events.push(event.type)
  })
  store.putGeneration({
    id: 'generation',
    operationId: 'operation',
    spec: { mode: 'text-to-image', params: {} },
    status: 'queued',
    version: 1,
  })
  store.patchGeneration('generation', 'operation', { status: 'running' }, 2)
  store.mutateTransient(() => undefined)
  assert.deepEqual(events, ['persistent', 'persistent', 'transient'])
})
