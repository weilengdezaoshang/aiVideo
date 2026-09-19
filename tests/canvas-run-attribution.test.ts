import test from 'node:test'
import assert from 'node:assert/strict'
import { createNode } from '../apps/web/canvas/state/node-model.js'

/** node-generation 对分镜服务端执行（runId 归属）的保护：完成回填交给执行控制器。 */
function withStubs(
  fn: (harness: {
    calls: { url: string; method: string }[]
    listeners: Record<string, (event: unknown) => void>
    emit: (type: string, data?: unknown) => void
    restore: () => void
  }) => Promise<void>,
) {
  const keys = ['fetch', 'EventSource', 'window', 'localStorage'] as const
  const previous = keys.map((key) => Object.getOwnPropertyDescriptor(globalThis, key))
  const calls: { url: string; method: string }[] = []
  const listeners: Record<string, (event: unknown) => void> = {}
  const memory = new Map<string, string>()
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key: string) => memory.get(key) || null,
      setItem: (key: string, value: string) => memory.set(key, value),
      removeItem: (key: string) => memory.delete(key),
    },
  })
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: { addEventListener() {}, removeEventListener() {} },
  })
  Object.defineProperty(globalThis, 'EventSource', {
    configurable: true,
    value: class {
      addEventListener(type: string, handler: (event: unknown) => void) {
        listeners[type] = handler
      }
      close() {}
    },
  })
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: (url: string, init?: RequestInit) => {
      calls.push({ url, method: init?.method || 'GET' })
      return Promise.resolve(Response.json({}, { status: 404 }))
    },
  })
  const restore = () => {
    keys.forEach((key, index) => {
      const descriptor = previous[index]
      if (descriptor) {
        Object.defineProperty(globalThis, key, descriptor)
      } else {
        Reflect.deleteProperty(globalThis, key)
      }
    })
  }
  return fn({
    calls,
    listeners,
    emit: (type, data) => listeners[type]?.({ data: JSON.stringify(data) }),
    restore,
  })
}

test('分镜归属的节点：完成的任务事件不直接落位，进度仍同步', async () => {
  await withStubs(async ({ listeners, emit, restore }) => {
    const { createDocStore } = await import('../apps/web/canvas/state/doc-store.js')
    const { createNodeGeneration } = await import('../apps/web/canvas/flows/node-generation.js')
    const node = createNode('image', { x: 0, y: 0 }, '镜头')
    node.nodeDraft!.prompt = '分镜镜头'
    const store = createDocStore({
      id: 'doc-owned',
      name: 'doc',
      objects: { [node.id]: node },
      order: [node.id],
    })
    const controller = createNodeGeneration(store, () => {})
    try {
      store.mutateTransient(() => {
        node.nodeRun = {
          requestId: 'run-owned-request',
          status: 'running',
          snapshot: structuredClone(node.nodeDraft!),
          runId: 'run-1',
        }
      })
      emit('job', {
        id: 'job-progress',
        documentId: 'doc-owned',
        clientRef: node.id,
        requestId: 'run-owned-request',
        status: 'running',
        progress: 0.5,
        message: '生成中',
      })
      assert.equal(node.nodeRun?.progress, 0.5)
      emit('job', {
        id: 'job-progress',
        documentId: 'doc-owned',
        clientRef: node.id,
        requestId: 'run-owned-request',
        status: 'completed',
        images: [
          {
            id: 'img',
            url: '/images/owned.png',
            file: 'owned.png',
            params: { kind: 'image', width: 64, height: 64 },
          },
        ],
      })
      assert.equal(node.src, undefined, '完成回填必须由执行控制器核对镜头归属')
      assert.equal(node.nodeRun?.status, 'running')
      assert.ok(listeners['open'])
    } finally {
      controller.dispose()
      restore()
    }
  })
})

test('分镜归属节点 404 不标记不确定，删除节点不误取消执行请求', async () => {
  await withStubs(async ({ calls, listeners, emit, restore }) => {
    const { createDocStore } = await import('../apps/web/canvas/state/doc-store.js')
    const { createNodeGeneration } = await import('../apps/web/canvas/flows/node-generation.js')
    const { cmdRemoveObjects } = await import('../apps/web/canvas/state/commands.js')
    const node = createNode('image', { x: 0, y: 0 }, '镜头')
    node.nodeDraft!.prompt = '分镜镜头'
    const store = createDocStore({
      id: 'doc-404',
      name: 'doc',
      objects: { [node.id]: node },
      order: [node.id],
    })
    const controller = createNodeGeneration(store, () => {})
    try {
      store.mutateTransient(() => {
        node.nodeRun = {
          requestId: 'pending-run-request',
          status: 'queued',
          snapshot: structuredClone(node.nodeDraft!),
          runId: 'run-pending',
          message: '分镜顺序执行中',
        }
      })
      assert.ok(listeners['open'])
      emit('open')
      await new Promise((resolve) => setImmediate(resolve))
      await new Promise((resolve) => setImmediate(resolve))
      assert.equal(node.nodeRun?.status, 'queued', '未提交的分镜请求不得标记为不确定')
      // 用户删除节点：执行请求由执行记录统一管理，不在此处误取消
      store.apply(cmdRemoveObjects([{ object: node, orderIndex: 0 }]))
      await new Promise((resolve) => setImmediate(resolve))
      assert.equal(
        calls.filter((call) => call.method === 'DELETE').length,
        0,
        '分镜归属请求不得被自动取消',
      )
    } finally {
      controller.dispose()
      restore()
    }
  })
})
