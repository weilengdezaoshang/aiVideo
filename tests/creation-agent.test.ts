import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createAgentStore } from '../apps/web/creation-agent/src/store.js'
import type { StreamEvent, Transport } from '../apps/web/creation-agent/src/model.js'
function fixture() {
  let emit: (event: StreamEvent) => void = () => {}
  let runId = ''
  let failResume = false
  let resumeCalls = 0
  const transport: Transport = {
    start(id, _complex, _signal, callback) {
      runId = id
      emit = callback
      return new Promise(() => {})
    },
    async resumeInterrupt() {
      resumeCalls++
      if (failResume) {
        throw new Error('恢复失败')
      }
    },
    stop() {},
  }
  const store = createAgentStore(transport)
  return {
    store,
    event: (event: Omit<StreamEvent, 'runId'>) => emit({ ...event, runId } as StreamEvent),
    setFail: (value: boolean) => {
      failResume = value
    },
    calls: () => resumeCalls,
  }
}
test('空白禁止发送、乐观更新、忙碌禁止发送、文本唯一', () => {
  const { store, event } = fixture()
  store.getState().send('   ')
  assert.equal(store.getState().messageIds.length, 0)
  store.getState().setDraft('咖啡')
  store.getState().send()
  assert.equal(store.getState().status, 'sending')
  assert.equal(store.getState().draft, '')
  store.getState().send('重复')
  assert.equal(store.getState().messageIds.length, 2)
  event({ type: 'stream_start' })
  event({ type: 'text_delta', text: '你' } as StreamEvent)
  event({ type: 'text_delta', text: '好' } as StreamEvent)
  const message = store.getState().messagesById[store.getState().messageIds[1]]
  assert.deepEqual(message.parts, [{ kind: 'text', text: '你好' }])
  event({ type: 'stream_complete' })
  assert.equal(store.getState().status, 'idle')
})
test('计划原地更新、确认防连点、恢复失败重试、同消息继续', async () => {
  const { store, event, setFail, calls } = fixture()
  store.getState().send('广告')
  event({ type: 'stream_start' })
  for (const status of ['pending', 'completed'] as const) {
    event({ type: 'plan_update', steps: [{ id: 'one', text: '策划', status }] } as StreamEvent)
  }
  event({
    type: 'confirm_required',
    confirm: {
      kind: 'confirm',
      confirmId: 'confirm',
      title: '确认',
      summary: '三张图',
      estimateCredits: 120,
      state: 'pending',
    },
  } as StreamEvent)
  assert.equal(store.getState().status, 'awaiting_user')
  setFail(true)
  await Promise.all([store.getState().confirm(true), store.getState().confirm(true)])
  assert.equal(calls(), 1)
  assert.equal(store.getState().status, 'awaiting_user')
  assert.equal(store.getState().confirming, false)
  setFail(false)
  await store.getState().confirm(true)
  assert.equal(store.getState().status, 'streaming')
  assert.equal(store.getState().messageIds.length, 2)
  const message = store.getState().messagesById[store.getState().messageIds[1]]
  assert.equal(message.parts.filter((p) => p.kind === 'plan').length, 1)
  assert.equal(message.parts.find((p) => p.kind === 'confirm')?.state, 'accepted')
})
test('停止隔离迟到事件，保留作品并取消执行中的工具', () => {
  const { store, event } = fixture()
  store.getState().send('图')
  event({ type: 'stream_start' })
  event({ type: 'tool_start', callId: 'a', label: '生成中' } as StreamEvent)
  event({
    type: 'tool_success',
    callId: 'a',
    artifact: {
      kind: 'artifact',
      id: 'image',
      type: 'image',
      url: '/coffee-1.svg',
      status: 'success',
    },
  } as StreamEvent)
  event({ type: 'tool_start', callId: 'b', label: '生成中' } as StreamEvent)
  store.getState().stop()
  const before = store.getState().messagesById
  event({ type: 'text_delta', text: '迟到' } as StreamEvent)
  event({ type: 'stream_complete' })
  assert.equal(store.getState().messagesById, before)
  const message = before[store.getState().messageIds[1]]
  assert.equal(message.status, 'cancelled')
  assert.equal(message.parts.filter((p) => p.kind === 'artifact').length, 1)
  assert.equal(message.parts.filter((p) => p.kind === 'tool_status').at(-1)?.state, 'cancelled')
})
test('拒绝确认后可重新发送', async () => {
  const { store, event } = fixture()
  store.getState().send('广告')
  event({ type: 'stream_start' })
  event({
    type: 'confirm_required',
    confirm: {
      kind: 'confirm',
      confirmId: 'c',
      title: '确认',
      summary: '',
      estimateCredits: 120,
      state: 'pending',
    },
  } as StreamEvent)
  await store.getState().confirm(false)
  assert.equal(store.getState().status, 'idle')
  const message = store.getState().messagesById[store.getState().messageIds[1]]
  assert.equal(message.parts.find((p) => p.kind === 'confirm')?.state, 'rejected')
  assert.match(message.parts.find((p) => p.kind === 'text')?.text ?? '', /未消耗积分/)
  store.getState().send('新作品')
  assert.equal(store.getState().messageIds.length, 4)
})
test('连接失败恢复草稿，工具失败返回 idle', async () => {
  const transport: Transport = {
    async start() {
      throw new Error('连接失败')
    },
    async resumeInterrupt() {},
    stop() {},
  }
  const store = createAgentStore(transport)
  store.getState().setDraft('原草稿')
  store.getState().send()
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(store.getState().draft, '原草稿')
  assert.equal(store.getState().status, 'idle')
  assert.equal(store.getState().toast, '连接失败')
  const other = fixture()
  other.store.getState().send('图')
  other.event({ type: 'stream_start' })
  other.event({ type: 'tool_start', callId: 'a', label: '生成' } as StreamEvent)
  other.event({ type: 'tool_failure', callId: 'a', message: '失败' } as StreamEvent)
  const message = other.store.getState().messagesById[other.store.getState().messageIds[1]]
  assert.equal(message.status, 'failed')
  assert.equal(other.store.getState().status, 'idle')
  assert.equal(message.parts.filter((p) => p.kind === 'artifact').length, 0)
})

test('真实 mock P0/P1 全流程：暂停、恢复、三张作品和计划完成', async () => {
  const { createMockTransport } = await import('../apps/web/creation-agent/src/mock.js')
  const store = createAgentStore(createMockTransport(0))
  const until = (predicate: () => boolean) =>
    new Promise<void>((resolve, reject) => {
      if (predicate()) {
        resolve()
        return
      }
      const timeout = setTimeout(() => {
        unsubscribe()
        reject(new Error('mock 流超时'))
      }, 3000)
      const unsubscribe = store.subscribe(() => {
        if (predicate()) {
          clearTimeout(timeout)
          unsubscribe()
          resolve()
        }
      })
    })
  store.getState().send('咖啡', false)
  await until(() => store.getState().status === 'idle')
  assert.equal(
    store
      .getState()
      .messagesById[store.getState().messageIds[1]].parts.filter((p) => p.kind === 'artifact')
      .length,
    1,
  )
  store.getState().send('广告', true)
  await until(() => store.getState().status === 'awaiting_user')
  const id = store.getState().messageIds[3]
  assert.equal(
    store.getState().messagesById[id].parts.filter((p) => p.kind === 'artifact').length,
    0,
  )
  await store.getState().confirm(true)
  await until(() => store.getState().status === 'idle')
  const message = store.getState().messagesById[id]
  assert.equal(message.status, 'completed')
  assert.equal(message.parts.filter((p) => p.kind === 'artifact').length, 3)
  assert.equal(message.parts.filter((p) => p.kind === 'text').length, 1)
  assert.equal(message.parts.filter((p) => p.kind === 'plan').length, 1)
  assert.equal(
    message.parts.find((p) => p.kind === 'plan')?.steps.every((s) => s.status === 'completed'),
    true,
  )
})

test('拒绝确认或停止时终止进行中的计划步骤，保留已完成步骤', async () => {
  for (const action of ['reject', 'stop', 'error'] as const) {
    const { store, event } = fixture()
    store.getState().send('广告')
    event({ type: 'stream_start' })
    event({
      type: 'plan_update',
      steps: [
        { id: 'done', text: '策划', status: 'completed' },
        { id: 'active', text: '生成', status: 'in_progress' },
        { id: 'later', text: '整理', status: 'pending' },
      ],
    } as StreamEvent)
    event({
      type: 'confirm_required',
      confirm: {
        kind: 'confirm',
        confirmId: 'c',
        title: '确认',
        summary: '',
        estimateCredits: 120,
        state: 'pending',
      },
    } as StreamEvent)
    if (action === 'reject') {
      await store.getState().confirm(false)
    } else if (action === 'stop') {
      store.getState().stop()
    } else {
      event({ type: 'stream_error', message: '失败' } as StreamEvent)
    }
    const message = store.getState().messagesById[store.getState().messageIds[1]]
    const plan = message.parts.find((part) => part.kind === 'plan')
    assert.deepEqual(
      plan?.steps.map((step) => step.status),
      ['completed', 'cancelled', 'pending'],
    )
    assert.equal(store.getState().status, 'idle')
    event({
      type: 'plan_update',
      steps: [{ id: 'active', text: '生成', status: 'in_progress' }],
    } as StreamEvent)
    assert.equal(store.getState().messagesById[message.id], message)
  }
})
