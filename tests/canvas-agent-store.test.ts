import './dom.js'
import test, { afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { AgentApiError, createAgentApi, type AgentApi } from '../apps/web/canvas/agent/api.js'
import { createAgentStore, type AgentEventSource } from '../apps/web/canvas/agent/store.js'
import type { AgentRequest, AgentRun, AgentSnapshot } from '../apps/web/canvas/agent/types.js'
import { createAutoSaver } from '../apps/web/canvas/state/autosave.js'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'

afterEach(() => localStorage.clear())
const tick = () => new Promise<void>((resolve) => setImmediate(resolve))
const snapshot = (runs: AgentRun[] = [], cursor = 0): AgentSnapshot => ({
  sessionId: 'session',
  documentId: 'doc',
  runs,
  cursor,
})
const makeRun = (request: AgentRequest, status: AgentRun['status'] = 'queued'): AgentRun => ({
  id: 'run-1',
  ...request,
  status,
  reply: null,
  error: null,
  modelCalls: [],
  toolCalls: [],
  createdAt: '2026-10-06T10:00:00Z',
  updatedAt: '2026-10-06T10:00:00Z',
})
function apiFor(overrides: Partial<AgentApi> = {}): AgentApi {
  return {
    models: async () => ({
      models: [{ id: 'deepseek', provider: 'deepseek', model: 'deepseek-chat', label: 'DeepSeek' }],
      defaultModelId: 'deepseek',
    }),
    snapshot: async () => snapshot(),
    create: async (_doc, request) => ({ run: makeRun(request) }),
    get: async () => {
      throw new Error('not used')
    },
    stop: async () => {
      throw new Error('not used')
    },
    ...overrides,
  }
}
class Events implements AgentEventSource {
  listeners = new Map<string, (event: { data?: string }) => void>()
  closed = false
  addEventListener(type: string, listener: (event: { data?: string }) => void) {
    this.listeners.set(type, listener)
  }
  close() {
    this.closed = true
  }
  emit(type: string, data?: unknown) {
    this.listeners.get(type)?.({ data: JSON.stringify(data) })
  }
}

test('助手消息与工具运行不写文档、不进撤销；clean画布无保存POST', async () => {
  const doc = createDocStore({
    id: 'doc',
    name: '画布',
    revision: 4,
    objects: {},
    order: [],
    chat: [{ id: 'old', at: '', role: 'user', kind: 'text', text: '旧消息' }],
  })
  const before = JSON.stringify(doc.doc)
  let posts = 0
  const saver = createAutoSaver({
    docId: 'doc',
    getDoc: () => doc.doc,
    fetchImpl: async () => {
      posts++
      return Response.json({})
    },
  })
  const events = new Events()
  const store = createAgentStore({
    documentId: 'doc',
    ensureSaved: () => saver.ensureSaved(),
    api: apiFor(),
    eventSourceFactory: () => events,
  })
  const close = store.start()
  try {
    await tick()
    store.setDraft('看看画布')
    assert.equal(await store.send([]), true)
    assert.equal(store.getSnapshot().draft, '')
    const run = store.getSnapshot().runs[0]
    events.emit('agent', {
      seq: 1,
      runId: run.id,
      type: 'completed',
      run: { ...run, status: 'completed', reply: '没有图片' },
    })
    assert.equal(store.getSnapshot().runs[0].reply, '没有图片')
    assert.equal(posts, 0)
    assert.equal(JSON.stringify(doc.doc), before)
    assert.equal(doc.canUndo(), false)
  } finally {
    close()
  }
})

test('发送前捕获selection并等待保存回执；保存失败保留输入且不建Run', async () => {
  let release!: (revision: number) => void
  const saving = new Promise<number>((resolve) => {
    release = resolve
  })
  const requests: AgentRequest[] = []
  const selection = ['source-a']
  const store = createAgentStore({
    documentId: 'doc',
    ensureSaved: () => saving,
    api: apiFor({
      create: async (_id, body) => {
        requests.push(body)
        return { run: makeRun(body) }
      },
    }),
    eventSourceFactory: () => new Events(),
  })
  const close = store.start()
  try {
    await tick()
    store.setDraft('检查选中对象')
    const send = store.send(selection)
    selection[0] = 'source-b'
    assert.equal(requests.length, 0)
    release(9)
    await send
    assert.deepEqual(requests[0]?.selectedObjectIds, ['source-a'])
    assert.equal(requests[0]?.baseRevision, 9)
  } finally {
    close()
  }
  let posts = 0
  const failed = createAgentStore({
    documentId: 'doc',
    ensureSaved: async () => {
      throw new Error('版本冲突')
    },
    api: apiFor({
      create: async (_id, body) => {
        posts++
        return { run: makeRun(body) }
      },
    }),
    eventSourceFactory: () => new Events(),
  })
  const dispose = failed.start()
  try {
    await tick()
    failed.setDraft('请检查')
    assert.equal(await failed.send([]), false)
    assert.equal(failed.getSnapshot().draft, '请检查')
    assert.match(failed.getSnapshot().error, /版本冲突/)
    assert.equal(posts, 0)
  } finally {
    dispose()
  }
})

test('POST响应丢失后刷新按原requestId找回已完成Run，不重发模型请求', async () => {
  let accepted: AgentRun | undefined
  let readable = false
  let posts = 0
  const lookups: (string | undefined)[] = []
  const api = apiFor({
    create: async (_id, request) => {
      posts++
      assert.ok(
        localStorage.getItem(`gencanvas.agent.pending.doc.${request.clientRequestId}`),
        '副作用前已独立持久化',
      )
      accepted = makeRun(request, 'completed')
      throw new Error('response lost')
    },
    snapshot: async (_id, requestId) => {
      lookups.push(requestId)
      return {
        ...snapshot(),
        requestedRun: readable && requestId === accepted?.clientRequestId ? accepted : null,
      }
    },
  })
  const first = createAgentStore({
    documentId: 'doc',
    ensureSaved: async () => 7,
    api,
    eventSourceFactory: () => new Events(),
  })
  const close = first.start()
  await tick()
  first.setDraft('读一下')
  await first.send([])
  assert.equal(first.getSnapshot().pendingRequests.length, 1)
  assert.equal(await first.send([]), false)
  close()
  readable = true
  const reopened = createAgentStore({
    documentId: 'doc',
    ensureSaved: async () => 99,
    api,
    eventSourceFactory: () => new Events(),
  })
  const dispose = reopened.start()
  try {
    await tick()
    assert.equal(reopened.getSnapshot().runs[0].status, 'completed')
    assert.equal(reopened.getSnapshot().pendingRequests.length, 0)
    assert.equal(reopened.getSnapshot().draft, '')
    assert.equal(lookups.at(-1), accepted?.clientRequestId)
    assert.equal(posts, 1)
  } finally {
    dispose()
  }
})

test('显式重试保留完整原请求，不能借重试变更模型、版本或selection', async () => {
  const submitted: AgentRequest[] = []
  const store = createAgentStore({
    documentId: 'doc',
    ensureSaved: async () => 5,
    api: apiFor({
      create: async (_id, body) => {
        submitted.push(structuredClone(body))
        if (submitted.length === 1) {
          throw new Error('offline')
        }
        return { run: makeRun(body) }
      },
    }),
    eventSourceFactory: () => new Events(),
  })
  const close = store.start()
  try {
    await tick()
    store.setDraft('原消息')
    await store.send(['a'])
    store.setDraft('新的输入保留')
    store.setModel('bailian')
    await store.retryPending(submitted[0].clientRequestId)
    assert.deepEqual(submitted[1], submitted[0])
    assert.equal(store.getSnapshot().draft, '新的输入保留')
    assert.equal(store.getSnapshot().pendingRequests.length, 0)
  } finally {
    close()
  }
})

test('明确拒绝保留输入，SSE重复和缺口仅对账，不重复提交', async () => {
  const sources: Events[] = []
  const urls: string[] = []
  let current = snapshot()
  let posts = 0
  const store = createAgentStore({
    documentId: 'doc',
    ensureSaved: async () => 2,
    api: apiFor({
      snapshot: async () => current,
      create: async () => {
        posts++
        throw new AgentApiError('当前画布已有运行', 409)
      },
    }),
    eventSourceFactory: (url) => {
      urls.push(url)
      const source = new Events()
      sources.push(source)
      return source
    },
  })
  const close = store.start()
  try {
    await tick()
    store.setDraft('我的问题')
    await store.send([])
    assert.equal(store.getSnapshot().pendingRequests.length, 0)
    assert.equal(store.getSnapshot().draft, '我的问题')
    const run = makeRun({
      clientRequestId: 'request-other',
      message: '另一个标签页',
      modelId: 'deepseek',
      baseRevision: 2,
      selectedObjectIds: [],
    })
    sources[0].emit('agent', { seq: 1, runId: run.id, type: 'created', run })
    sources[0].emit('agent', { seq: 1, runId: run.id, type: 'created', run })
    assert.equal(store.getSnapshot().runs.length, 1)
    current = snapshot([{ ...run, status: 'completed', reply: '结果' }], 3)
    sources[0].emit('agent', { seq: 3, runId: run.id, type: 'completed', run })
    await tick()
    assert.equal(store.getSnapshot().cursor, 3)
    assert.equal(store.getSnapshot().runs[0].reply, '结果')
    assert.match(urls.at(-1) ?? '', /after=3$/)
    assert.equal(posts, 1)
  } finally {
    close()
  }
})

test('StrictMode重挂载忽略旧快照并正常完成新加载', async () => {
  let resolveOld!: (value: AgentSnapshot) => void
  let calls = 0
  const store = createAgentStore({
    documentId: 'doc',
    ensureSaved: async () => 1,
    api: apiFor({
      snapshot: async () => {
        calls++
        if (calls === 1) {
          return new Promise((resolve) => {
            resolveOld = resolve
          })
        }
        return snapshot([], 4)
      },
    }),
    eventSourceFactory: () => new Events(),
  })
  const first = store.start()
  first()
  const second = store.start()
  try {
    await tick()
    resolveOld(snapshot([], 1))
    await tick()
    assert.equal(store.getSnapshot().cursor, 4)
    assert.equal(store.getSnapshot().loading, false)
  } finally {
    second()
  }
})

test('Agent API mutation使用同源CSRF，错误体保持可读', async () => {
  document.cookie = '__Host-aivideo-csrf=test-token; Secure; Path=/'
  const api = createAgentApi(async (_url, init) => {
    assert.equal(new Headers(init?.headers).get('X-CSRF-Token'), 'test-token')
    return Response.json({ detail: '权限不足' }, { status: 403 })
  })
  await assert.rejects(api.stop('run'), /权限不足/)
})

test('迟到的快照或同毫秒事件不能覆盖较新的停止回执；SSE支持快照恢复', async () => {
  const request: AgentRequest = {
    clientRequestId: 'r',
    message: '读取',
    modelId: 'deepseek',
    baseRevision: 1,
    selectedObjectIds: [],
  }
  const running = { ...makeRun(request, 'running'), stateVersion: 2 }
  const stopped = { ...makeRun(request, 'stopped'), stateVersion: 4 }
  const events = new Events()
  const store = createAgentStore({
    documentId: 'doc',
    ensureSaved: async () => 1,
    api: apiFor({
      snapshot: async () => snapshot([running], 2),
      stop: async () => ({ run: stopped }),
    }),
    eventSourceFactory: () => events,
  })
  const close = store.start()
  try {
    await tick()
    await store.stop(stopped.id)
    await store.refresh()
    assert.equal(store.getSnapshot().runs[0]?.status, 'stopped')
    events.emit('agent', {
      seq: 3,
      runId: running.id,
      type: 'tool.started',
      run: { ...running, stateVersion: 3 },
    })
    assert.equal(store.getSnapshot().runs[0]?.status, 'stopped')
    events.emit('snapshot', snapshot([stopped], 4))
    assert.equal(store.getSnapshot().cursor, 4)
    assert.equal(store.getSnapshot().runs[0]?.stateVersion, 4)
  } finally {
    close()
  }
})
