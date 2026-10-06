import test from 'node:test'
import assert from 'node:assert/strict'
import {
  createNode,
  applyNodeJob,
  migrateNodes,
  outputSize,
  isEmptyMedia,
} from '../apps/web/canvas/state/node-model.js'
import type { NodeJob } from '../apps/web/canvas/state/node-model.js'

test('三个节点保持类型、独立草稿和固定文本卡片', () => {
  const image = createNode('image', { x: 10, y: 20 }, '图片')
  const other = createNode('image', { x: 0, y: 0 }, '图片2')
  image.nodeDraft!.prompt = '红色'
  assert.equal(other.nodeDraft!.prompt, '')
  assert.equal(image.kind, 'image')
  assert.equal(createNode('video', { x: 0, y: 0 }, '视频').width, 480)
  const text = createNode('text', { x: 0, y: 0 }, '文本')
  assert.equal(text.textCard, true)
  assert.equal(text.text, '')
  assert.deepEqual(outputSize({ ...image.nodeDraft!, ratio: '16:9', resolution: 1024 }), {
    width: 1024,
    height: 576,
  })
})

test('结果只回填对应画布、节点与请求，迟到事件不能覆盖结果', () => {
  const obj = createNode('image', { x: 10, y: 20 }, '图片')
  obj.nodeRun = {
    requestId: 'current',
    status: 'running',
    snapshot: structuredClone(obj.nodeDraft!),
  }
  const job: NodeJob = {
    id: 'job',
    clientRef: obj.id,
    documentId: 'doc',
    requestId: 'current',
    status: 'completed',
    images: [
      {
        id: 'result',
        url: '/output/result.png',
        file: 'result.png',
        params: { kind: 'image', width: 1024, height: 576 },
      },
    ],
  }
  assert.equal(applyNodeJob(obj, { ...job, requestId: 'old' }, 'doc'), false)
  assert.equal(applyNodeJob(obj, job, 'other-doc'), false)
  assert.equal(applyNodeJob(obj, { ...job, clientRef: 'other-node' }, 'doc'), false)
  assert.equal(applyNodeJob(obj, job, 'doc'), true)
  assert.equal(obj.x, 10)
  assert.equal(obj.width, 320)
  assert.equal(obj.height, 180)
  assert.equal(obj.nodeRun, undefined)
  assert.equal(applyNodeJob(obj, { ...job, status: 'failed' }, 'doc'), false)
  assert.equal(obj.src, '/output/result.png')
})

test('旧草稿加载时迁移为稳定媒体节点', () => {
  const obj = createNode('video', { x: 0, y: 0 }, '视频')
  obj.kind = 'draft'
  obj.mediaType = 'video'
  delete obj.nodeDraft
  assert.equal(migrateNodes({ [obj.id]: obj }), true)
  assert.equal(obj.kind, 'video')
  assert.equal(
    (obj as import('../apps/web/canvas/state/commands.js').CanvasObj).nodeDraft?.ratio,
    '16:9',
  )
  assert.equal(migrateNodes({ [obj.id]: obj }), false)
})

test('撤销提交会取消原请求，重做只查询状态，迟到结果不会恢复已撤销任务', async () => {
  const { createDocStore } = await import('../apps/web/canvas/state/doc-store.js')
  const { createNodeGeneration } = await import('../apps/web/canvas/flows/node-generation.js')
  const keys = ['fetch', 'EventSource', 'window', 'localStorage'] as const
  const previous = keys.map((key) => Object.getOwnPropertyDescriptor(globalThis, key))
  let finishRequest!: (response: Response) => void
  const calls: { url: string; method: string }[] = []
  const cancelled = new Set<string>()
  const memory = new Map<string, string>()
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key: string) => memory.get(key) || null,
      setItem: (key: string, value: string) => memory.set(key, value),
    },
  })
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: { addEventListener() {}, removeEventListener() {} },
  })
  Object.defineProperty(globalThis, 'EventSource', {
    configurable: true,
    value: class {
      addEventListener() {}
      close() {}
    },
  })
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: (url: string, init?: RequestInit) => {
      const method = init?.method || 'GET'
      calls.push({ url, method })
      if (method === 'POST') {
        return new Promise<Response>((resolve) => {
          finishRequest = resolve
        })
      }
      if (method === 'DELETE') {
        cancelled.add(url)
      }
      return Promise.resolve(
        Response.json(cancelled.has(url) ? { cancelled: true } : {}, {
          status: cancelled.has(url) ? 200 : 404,
        }),
      )
    },
  })
  let controller: ReturnType<typeof createNodeGeneration> | undefined
  try {
    const obj = createNode('image', { x: 0, y: 0 }, '图片')
    obj.nodeDraft!.prompt = '测试'
    obj.nodeDraft!.model = 'mock-diffusion-xl'
    const store = createDocStore({
      id: 'doc',
      name: '测试',
      objects: { [obj.id]: obj },
      order: [obj.id],
    })
    controller = createNodeGeneration(store, () => {})
    const pending = controller.submit(obj)
    const requestId = obj.nodeRun!.requestId
    store.undo()
    await new Promise((resolve) => setImmediate(resolve))
    assert.equal(calls.filter((call) => call.method === 'DELETE').length, 1)
    finishRequest(
      Response.json({
        job: {
          id: 'job',
          documentId: 'doc',
          clientRef: obj.id,
          requestId,
          status: 'completed',
          images: [],
        },
      }),
    )
    await pending
    assert.equal(obj.nodeRun, undefined)
    assert.equal(obj.src, undefined)
    store.redo()
    await new Promise((resolve) => setImmediate(resolve))
    assert.equal(obj.nodeRun, undefined)
    assert.equal(calls.filter((call) => call.method === 'POST').length, 1)
  } finally {
    controller?.dispose()
    keys.forEach((key, i) => {
      const descriptor = previous[i]
      if (descriptor) {
        Object.defineProperty(globalThis, key, descriptor)
      } else {
        Reflect.deleteProperty(globalThis, key)
      }
    })
  }
})

test('生成面板只适用于空图片和视频，结果即使保留草稿也不显示', () => {
  for (const kind of ['image', 'video'] as const) {
    const node = createNode(kind, { x: 0, y: 0 }, kind)
    assert.equal(isEmptyMedia(node), true)
    node.src = '/images/result.webp'
    assert.equal(isEmptyMedia(node), false)
    delete node.src
    node.assetId = 'uploaded-asset'
    assert.equal(isEmptyMedia(node), false)
  }
  assert.equal(isEmptyMedia(createNode('text', { x: 0, y: 0 }, '文本')), false)
})

test('新建元素使用固定画布尺寸，保存和恢复不会引入视口缩放', () => {
  for (const kind of ['image', 'video', 'text'] as const) {
    const node = createNode(kind, { x: 125, y: -80 }, kind)
    const restored = JSON.parse(JSON.stringify(node)) as typeof node
    assert.equal(restored.width, kind === 'video' ? 480 : 320)
    assert.equal(restored.height, kind === 'video' ? 270 : 320)
    assert.deepEqual({ x: restored.x, y: restored.y }, { x: 125, y: -80 })
    if (kind !== 'text') {
      assert.equal(restored.nodeDraft!.resolution, kind === 'video' ? 512 : 1024)
    }
  }
})

test('旧版反向放大的空占位只修正一次，5% 下图片为 16px', () => {
  const node = createNode('image', { x: 10, y: 20 }, '旧占位')
  delete node.canvasSizeVersion
  node.width = node.height = 2400
  assert.equal(migrateNodes({ [node.id]: node }), true)
  assert.equal(node.width * 0.05, 16)
  assert.equal(node.height * 0.05, 16)
  node.width = node.height = 600
  assert.equal(migrateNodes({ [node.id]: node }), false)
  assert.equal(node.width, 600)
  const output = createNode('image', { x: 0, y: 0 }, '已有结果')
  delete output.canvasSizeVersion
  output.src = '/images/result.png'
  output.width = 1024
  migrateNodes({ [output.id]: output })
  assert.equal(output.width, 1024)
})
