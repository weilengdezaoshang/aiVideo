import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createAgentStore } from '../apps/web/creation-agent/src/store.js'
import { createServerTransport } from '../apps/web/creation-agent/src/server-transport.js'
import { createApplication } from '../apps/server/src/app/create-app.js'
import { MemoryTraceWriter } from '../apps/server/src/adapters/memory/index.js'
function waitFor(store: ReturnType<typeof createAgentStore>, predicate: () => boolean) {
  return new Promise<void>((resolve, reject) => {
    if (predicate()) {
      resolve()
      return
    }
    const timeout = setTimeout(() => {
      unsubscribe()
      reject(new Error('界面状态超时'))
    }, 3000)
    const unsubscribe = store.subscribe(() => {
      if (predicate()) {
        clearTimeout(timeout)
        unsubscribe()
        resolve()
      }
    })
  })
}
test('真实 SSE → store：确认继续/取消都更新同一助手，计划唯一', async () => {
  const original = globalThis.fetch
  try {
    for (const approved of [true, false]) {
      const app = createApplication({
        ports: { traces: new MemoryTraceWriter() },
        llmDelayMs: 0,
        toolDelayMs: 0,
      })
      globalThis.fetch = async (input, init) =>
        app.api.fetch(new Request(String(input), { ...init, keepalive: false }))
      const store = createAgentStore(createServerTransport())
      store.getState().send('做一条咖啡广告，3个镜头')
      await waitFor(store, () => store.getState().status === 'awaiting_user')
      const id = store.getState().messageIds[1]
      const pending = store.getState().messagesById[id]
      assert.equal(pending.parts.filter((p) => p.kind === 'plan').length, 1)
      assert.equal(pending.parts.filter((p) => p.kind === 'confirm').length, 1)
      assert.equal(pending.parts.filter((p) => p.kind === 'artifact').length, 0)
      await store.getState().confirm(approved)
      await waitFor(store, () => store.getState().status === 'idle')
      const final = store.getState().messagesById[id]
      assert.equal(store.getState().messageIds.length, 2)
      assert.equal(final.parts.filter((p) => p.kind === 'plan').length, 1)
      assert.equal(final.parts.filter((p) => p.kind === 'confirm').length, 1)
      assert.equal(
        final.parts.find((p) => p.kind === 'confirm')?.state,
        approved ? 'accepted' : 'rejected',
      )
      assert.equal(final.parts.filter((p) => p.kind === 'artifact').length, approved ? 3 : 0)
      assert.equal(
        final.parts.find((p) => p.kind === 'plan')?.steps.some((s) => s.status === 'in_progress'),
        false,
      )
      assert.equal(store.getState().confirming, false)
      if (!approved) {
        assert.match(final.parts.find((p) => p.kind === 'text')!.text, /未消耗积分/)
      }
    }
  } finally {
    globalThis.fetch = original
  }
})
test('恢复前网络失败保留 pending 与 awaiting_user，重试后完成', async () => {
  const original = globalThis.fetch
  const app = createApplication({
    ports: { traces: new MemoryTraceWriter() },
    llmDelayMs: 0,
    toolDelayMs: 0,
  })
  globalThis.fetch = async (input, init) =>
    app.api.fetch(new Request(String(input), { ...init, keepalive: false }))
  try {
    const store = createAgentStore(createServerTransport())
    store.getState().send('三镜头广告')
    await waitFor(store, () => store.getState().status === 'awaiting_user')
    store.getState().setFault('resume', true)
    await store.getState().confirm(true)
    assert.equal(store.getState().status, 'awaiting_user')
    assert.equal(store.getState().confirming, false)
    const message = store.getState().messagesById[store.getState().messageIds[1]]
    assert.equal(message.parts.find((p) => p.kind === 'confirm')?.state, 'pending')
    await store.getState().confirm(true)
    assert.equal(store.getState().status, 'idle')
  } finally {
    globalThis.fetch = original
  }
})

test('批准积分后工具再次中断，第二张确认卡可继续，同一计划与消息不丢失', async () => {
  const original = globalThis.fetch
  let executions = 0
  const app = createApplication({
    ports: { traces: new MemoryTraceWriter() },
    llmDelayMs: 0,
    tools: [
      {
        name: 'creative_agent_mcp_gen_text2image_v3',
        estimateCredits: 40,
        async execute() {
          executions++
          if (executions === 1) {
            return { need_user: true, estimateCredits: 40, summary: '请确认继续该工具' }
          }
          return {
            artifact: {
              kind: 'artifact',
              id: `image-${executions}`,
              type: 'image',
              url: '/mock.svg',
              status: 'success',
            },
            credits: 40,
          }
        },
      },
    ],
  })
  globalThis.fetch = async (input, init) =>
    app.api.fetch(new Request(String(input), { ...init, keepalive: false }))
  try {
    const store = createAgentStore(createServerTransport())
    store.getState().send('三镜头广告')
    await waitFor(store, () => store.getState().status === 'awaiting_user')
    await store.getState().confirm(true)
    assert.equal(store.getState().status, 'awaiting_user')
    assert.equal(store.getState().confirming, false)
    const id = store.getState().messageIds[1]
    const cards = store.getState().messagesById[id].parts.filter((p) => p.kind === 'confirm')
    assert.deepEqual(
      cards.map((card) => card.state),
      ['accepted', 'pending'],
    )
    await store.getState().confirm(true)
    assert.equal(store.getState().status, 'idle')
    assert.equal(store.getState().messageIds.length, 2)
    assert.equal(
      store.getState().messagesById[id].parts.filter((p) => p.kind === 'artifact').length,
      3,
    )
    assert.equal(store.getState().messagesById[id].parts.filter((p) => p.kind === 'plan').length, 1)
    assert.equal(executions, 4)
  } finally {
    globalThis.fetch = original
  }
})

test('真实对话：取消后普通发送继续，恢复原计划并重新确认后完成', async () => {
  const original = globalThis.fetch
  const app = createApplication({
    ports: { traces: new MemoryTraceWriter() },
    llmDelayMs: 0,
    toolDelayMs: 0,
  })
  globalThis.fetch = async (input, init) =>
    app.api.fetch(new Request(String(input), { ...init, keepalive: false }))
  try {
    const store = createAgentStore(createServerTransport())
    store.getState().send('三镜头咖啡广告')
    await waitFor(store, () => store.getState().status === 'awaiting_user')
    const before = store
      .getState()
      .messagesById[store.getState().messageIds[1]].parts.find((p) => p.kind === 'plan')
    await store.getState().confirm(false)
    await waitFor(store, () => store.getState().status === 'idle')
    store.getState().send('继续')
    await waitFor(store, () => store.getState().status === 'awaiting_user')
    const id = store.getState().messageIds[3]
    const resumed = store.getState().messagesById[id]
    assert.equal(resumed.parts.filter((p) => p.kind === 'plan').length, 1)
    assert.deepEqual(
      resumed.parts.find((p) => p.kind === 'plan')?.steps.map((s) => s.id),
      before?.steps.map((s) => s.id),
    )
    assert.equal(resumed.parts.find((p) => p.kind === 'confirm')?.estimateCredits, 120)
    assert.equal(resumed.parts.filter((p) => p.kind === 'artifact').length, 0)
    await store.getState().confirm(true)
    await waitFor(store, () => store.getState().status === 'idle')
    assert.equal(
      store.getState().messagesById[id].parts.filter((p) => p.kind === 'artifact').length,
      3,
    )
    assert.ok(
      store
        .getState()
        .messagesById[id].parts.find((p) => p.kind === 'plan')
        ?.steps.every((s) => s.status === 'completed'),
    )
  } finally {
    globalThis.fetch = original
  }
})

test('P1 本地演示取消/停止后普通继续仍留在 mock 并恢复计划', async () => {
  const { createMockTransport } = await import('../apps/web/creation-agent/src/mock.js')
  for (const stop of [false, true]) {
    const store = createAgentStore(createMockTransport(0))
    store.getState().send('三镜头咖啡广告', true, 'mock')
    await waitFor(store, () => store.getState().status === 'awaiting_user')
    if (stop) {
      store.getState().stop()
    } else {
      await store.getState().confirm(false)
    }
    await new Promise<void>((resolve) => setTimeout(resolve, 0))
    store.getState().send('继续')
    await waitFor(store, () => store.getState().status === 'awaiting_user')
    const message = store.getState().messagesById[store.getState().messageIds[3]]
    assert.equal(store.getState().mode, 'mock')
    assert.equal(message.parts.filter((p) => p.kind === 'plan').length, 1)
    assert.equal(message.parts.find((p) => p.kind === 'confirm')?.estimateCredits, 120)
    await store.getState().confirm(true)
    await waitFor(store, () => store.getState().status === 'idle')
    assert.equal(
      store
        .getState()
        .messagesById[store.getState().messageIds[3]].parts.filter((p) => p.kind === 'artifact')
        .length,
      3,
    )
  }
})
