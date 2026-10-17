import { test } from 'node:test'
import { isContinueIntent } from '../src/domain/conversation-service.js'
import assert from 'node:assert/strict'
import { randomUUID } from 'node:crypto'
import { createApplication } from '../src/app/create-app.js'
import { MemorySessionStore, MemoryTraceWriter } from '../src/adapters/memory/index.js'
import type { LoggedEvent } from '../src/domain/model.js'
import type { Tool } from '../src/ports/index.js'
import { toolName } from '../src/tools/text2image.js'
import { agentTraceV1Schema } from '../src/trace/schema.js'
function request(body: object) {
  return {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }
}
function parse(text: string): LoggedEvent[] {
  return text.split('\n\n').flatMap((frame) => {
    const data = frame.split('\n').find((line) => line.startsWith('data:'))
    return data ? [JSON.parse(data.slice(5)) as LoggedEvent] : []
  })
}
function setup(options: Parameters<typeof createApplication>[0] = {}) {
  const traces = new MemoryTraceWriter()
  return {
    traces,
    ...createApplication({
      llmDelayMs: 0,
      streamCharDelayMs: 0,
      toolDelayMs: 0,
      ...options,
      ports: { traces, ...options.ports },
    }),
  }
}
async function initial(app: ReturnType<typeof setup>, prompt = '做一条咖啡广告，3个镜头') {
  const response = await app.api.request('/v1/conversation', request({ prompt }))
  assert.equal(response.status, 200)
  const events = parse(await response.text())
  const interruption = events.find((e) => e.kind === 'system' && e.type === 'interrupt_approval')
  assert.ok(
    interruption && interruption.kind === 'system' && interruption.type === 'interrupt_approval',
  )
  const body = {
    conversation_id: interruption.conversation_id,
    action: 'resume_interrupt',
    confirmId: interruption.confirmId,
  }
  return { events, interruption, body }
}
function finalMessage(events: LoggedEvent[]) {
  return events.filter((e) => e.kind === 'message').at(-1)!.message
}
test('复杂路径中断关闭 SSE；snapshot + plan + Trace v1 完备', async () => {
  const app = setup()
  const { events, interruption } = await initial(app)
  assert.equal(
    events.at(-1)!.kind === 'system' && (events.at(-1) as typeof interruption).type,
    'interrupt_approval',
  )
  assert.equal(
    events.some((e) => e.kind === 'system' && e.type === 'stream_complete'),
    false,
  )
  assert.equal(
    events.filter((e) => e.kind === 'system' && e.type === 'todo_plan').length >= 2,
    true,
  )
  assert.equal(finalMessage(events).parts.filter((p) => p.kind === 'plan').length, 1)
  assert.equal(
    finalMessage(events).parts.some((p) => p.kind === 'artifact'),
    false,
  )
  const record = await app.ports.sessions.getTurn(interruption.turn_uuid)
  assert.equal(record?.status, 'interrupted')
  const snapshot = await app.ports.sessions.getInterruptSnapshot(interruption.turn_uuid)
  assert.equal(snapshot?.confirmId, interruption.confirmId)
  assert.equal(snapshot?.activeAgent, 'planner')
  assert.equal(snapshot?.cursor?.lastEventSeq, interruption.sequence)
  assert.equal(snapshot?.continuation?.calls.length, 3)
  assert.equal(await app.ports.queue.getByIdempotencyKey(snapshot!.pendingToolCall!.id), null)
  const trace = agentTraceV1Schema.parse(app.traces.get(interruption.runId))
  assert.equal(trace.turns[0].outcome, 'interrupted')
  assert.equal(trace.turns[0].interrupts[0].resumed, false)
  assert.equal(
    trace.turns[0].toolCalls.some((t) => t.name === 'handoff_to_planner'),
    true,
  )
})
test('批准后同 turn/message 生成三张图，计划原地完成，Trace 含往返 handoff', async () => {
  const app = setup()
  const { interruption, body, events } = await initial(app)
  const output = parse(
    await (await app.api.request('/v1/conversation', request({ ...body, approved: true }))).text(),
  )
  assert.equal(output[0].turn_uuid, interruption.turn_uuid)
  assert.equal(output[0].runId, interruption.runId)
  assert.equal(finalMessage(output).id, finalMessage(events).id)
  assert.equal(finalMessage(output).parts.filter((p) => p.kind === 'artifact').length, 3)
  assert.equal(finalMessage(output).parts.filter((p) => p.kind === 'plan').length, 1)
  assert.equal(
    finalMessage(output)
      .parts.find((p) => p.kind === 'plan')
      ?.steps.every((s) => s.status === 'completed'),
    true,
  )
  assert.equal(finalMessage(output).parts.find((p) => p.kind === 'confirm')?.state, 'accepted')
  assert.equal(
    output.some((e) => e.kind === 'system' && e.type === 'stream_complete'),
    true,
  )
  assert.equal(await app.ports.sessions.getInterruptSnapshot(interruption.turn_uuid), null)
  const trace = agentTraceV1Schema.parse(app.traces.get(interruption.runId))
  const turn = trace.turns[0]
  assert.equal(turn.outcome, 'success')
  assert.equal(turn.metrics.credits, 120)
  assert.equal(turn.interrupts[0].resumed, true)
  assert.deepEqual(
    turn.agents.map((a) => a.name),
    ['host', 'planner'],
  )
  assert.equal(
    turn.toolCalls.some((t) => t.name === 'handoff_to_host'),
    true,
  )
  const generated = turn.toolCalls.filter((t) => t.name === toolName)
  assert.equal(generated.length, 3)
  assert.equal(new Set(generated.map((t) => t.jobId)).size, 3)
  assert.equal(
    generated.every((t) => t.status === 'finished' && t.credits === 40),
    true,
  )
  assert.equal(
    (await app.ports.sessions.getConversation(interruption.conversation_id))?.activeAgent,
    'host',
  )
})
test('取消确认无新图、无积分，清除 snapshot，终止计划；重复取消安全', async () => {
  const app = setup()
  const { body, interruption } = await initial(app)
  for (let repeat = 0; repeat < 2; repeat++) {
    const response = await app.api.request(
      '/v1/conversation',
      request({ ...body, approved: false }),
    )
    assert.equal(response.status, 200)
    const events = parse(await response.text())
    const message = finalMessage(events)
    assert.equal(message.status, 'cancelled')
    assert.equal(
      message.parts.some((p) => p.kind === 'artifact'),
      false,
    )
    assert.match(message.parts.find((p) => p.kind === 'text')!.text, /已取消，未消耗积分/)
    assert.equal(message.parts.find((p) => p.kind === 'confirm')?.state, 'rejected')
    assert.equal(
      message.parts.find((p) => p.kind === 'plan')?.steps.some((s) => s.status === 'in_progress'),
      false,
    )
  }
  assert.equal(await app.ports.sessions.getInterruptSnapshot(interruption.turn_uuid), null)
  const trace = app.traces.get(interruption.runId)!.turns[0]
  assert.equal(trace.outcome, 'cancelled')
  assert.equal(trace.metrics.credits, 0)
  assert.equal(trace.interrupts[0].resumed, false)
})
test('重复/并发批准不双跑；相反决定拒绝', async () => {
  let count = 0
  const tool: Tool = {
    name: toolName,
    estimateCredits: 40,
    async execute() {
      count++
      return {
        artifact: {
          kind: 'artifact',
          id: randomUUID(),
          type: 'image',
          url: '/mock.svg',
          status: 'success',
        },
        credits: 40,
      }
    },
  }
  const app = setup({ tools: [tool] })
  const { body } = await initial(app)
  const first = await app.api.request('/v1/conversation', request({ ...body, approved: true }))
  // 第一条 SSE 尚在推进：并发确认应拒绝，不能 enqueue 第二份任务。
  const concurrent = await app.api.request('/v1/conversation', request({ ...body, approved: true }))
  assert.equal(concurrent.status, 409)
  const firstEvents = parse(await first.text())
  assert.equal(count, 3)
  const duplicate = await app.api.request('/v1/conversation', request({ ...body, approved: true }))
  assert.equal(duplicate.status, 200)
  const secondEvents = parse(await duplicate.text())
  assert.equal(count, 3)
  assert.deepEqual(finalMessage(firstEvents).parts, finalMessage(secondEvents).parts)
  assert.equal(
    (await app.api.request('/v1/conversation', request({ ...body, approved: false }))).status,
    409,
  )
})
test('重新组装服务并只拷贝 snapshot/记录仍可恢复，无 Host 实例依赖', async () => {
  const original = setup()
  const { interruption, body } = await initial(original)
  const sessions = new MemorySessionStore()
  await sessions.saveConversation(
    (await original.ports.sessions.getConversation(interruption.conversation_id))!,
  )
  const record = (await original.ports.sessions.getTurn(interruption.turn_uuid))!
  // 不拷贝 runtime/controller，也不依赖记录中的 execution，唯一续执行来源是 snapshot。
  record.execution = undefined
  await sessions.saveTurn(record)
  const fresh = setup({ ports: { sessions, events: original.ports.events } })
  const response = await fresh.api.request('/v1/conversation', request({ ...body, approved: true }))
  assert.equal(response.status, 200)
  assert.equal(
    finalMessage(parse(await response.text())).parts.filter((p) => p.kind === 'artifact').length,
    3,
  )
})
test('高消耗单图先中断；阈值边界等于50时仍直出，无 handoff', async () => {
  const app = setup({ imageCredits: 60 })
  const { body, interruption } = await initial(app, '画一只猫')
  assert.equal(interruption.estimateCredits, 60)
  assert.equal(
    app.traces
      .get(interruption.runId)!
      .turns[0].toolCalls.some((t) => t.name.startsWith('handoff_')),
    false,
  )
  const events = parse(
    await (await app.api.request('/v1/conversation', request({ ...body, approved: true }))).text(),
  )
  assert.equal(finalMessage(events).parts.filter((p) => p.kind === 'artifact').length, 1)
  const boundary = setup({ imageCredits: 50 })
  const direct = parse(
    await (await boundary.api.request('/v1/conversation', request({ prompt: '画一只猫' }))).text(),
  )
  assert.equal(
    direct.some((e) => e.kind === 'system' && e.type === 'interrupt_approval'),
    false,
  )
  assert.equal(finalMessage(direct).parts.filter((p) => p.kind === 'artifact').length, 1)
})
test('工具返回 need_user 同样中断，批准只执行新的续调用，不复用询问 job', async () => {
  let count = 0
  const tool: Tool = {
    name: toolName,
    estimateCredits: 40,
    async execute(input) {
      count++
      if (!input.approved) {
        return { need_user: true, estimateCredits: 20, summary: '需要批准后继续生成' }
      }
      return {
        artifact: {
          kind: 'artifact',
          id: 'approved-image',
          type: 'image',
          url: '/mock.svg',
          status: 'success',
        },
        credits: 20,
      }
    },
  }
  const app = setup({ tools: [tool] })
  const { body, interruption } = await initial(app, '画猫')
  assert.equal(count, 1)
  const events = parse(
    await (await app.api.request('/v1/conversation', request({ ...body, approved: true }))).text(),
  )
  assert.equal(count, 2)
  assert.equal(finalMessage(events).parts.filter((p) => p.kind === 'artifact').length, 1)
  const trace = app.traces.get(interruption.runId)!.turns[0]
  assert.equal(trace.metrics.credits, 20)
  assert.equal(trace.interrupts[0].reason, 'approval')
  assert.equal(trace.interrupts[0].resumed, true)
})
test('未知确认/跨会话拒绝；等待确认时 stop 清除 snapshot 并写 cancelled Trace', async () => {
  const app = setup()
  const { body, interruption } = await initial(app)
  assert.equal(
    (
      await app.api.request(
        '/v1/conversation',
        request({ ...body, conversation_id: randomUUID(), approved: true }),
      )
    ).status,
    404,
  )
  assert.equal(
    (
      await app.api.request(
        '/v1/conversation',
        request({ ...body, confirmId: randomUUID(), approved: true }),
      )
    ).status,
    404,
  )
  const stop = await app.api.request(
    '/v1/stop_stream',
    request({ conversation_id: interruption.conversation_id, turn_uuid: interruption.turn_uuid }),
  )
  assert.equal(stop.status, 200)
  assert.equal(await app.ports.sessions.getInterruptSnapshot(interruption.turn_uuid), null)
  assert.equal(app.traces.get(interruption.runId)!.turns[0].outcome, 'cancelled')
  assert.equal(
    (await app.api.request('/v1/conversation', request({ ...body, approved: true }))).status,
    404,
  )
})

test('resume 激活前的停止不能重新启动生成', async () => {
  class CancelOnActivate extends MemorySessionStore {
    override async activateTurn(turn: import('../src/domain/model.js').Turn) {
      await this.cancel(turn.conversationId, turn.turnId)
      await super.activateTurn(turn)
    }
  }
  const app = setup({ ports: { sessions: new CancelOnActivate() } })
  const { body, interruption } = await initial(app)
  const response = await app.api.request('/v1/conversation', request({ ...body, approved: true }))
  const events = parse(await response.text())
  assert.equal(finalMessage(events).status, 'cancelled')
  assert.equal(
    finalMessage(events).parts.some((p) => p.kind === 'artifact'),
    false,
  )
  assert.equal(app.traces.get(interruption.runId)!.turns[0].outcome, 'cancelled')
})

test('取消保存 draft；继续跨轮恢复同一计划并重新确认，明确新任务清除 draft', async () => {
  const app = setup()
  const { body, interruption } = await initial(app)
  await (await app.api.request('/v1/conversation', request({ ...body, approved: false }))).text()
  const draft = await app.ports.sessions.getContinuationDraft(body.conversation_id)
  assert.ok(draft)
  assert.equal(draft.execution.index, 0)
  const events = parse(
    await (
      await app.api.request(
        '/v1/conversation',
        request({
          conversation_id: body.conversation_id,
          prompt: '继续',
        }),
      )
    ).text(),
  )
  const next = events.find((e) => e.kind === 'system' && e.type === 'interrupt_approval')
  assert.ok(next && next.kind === 'system' && next.type === 'interrupt_approval')
  assert.notEqual(next.turn_uuid, interruption.turn_uuid)
  assert.notEqual(next.runId, interruption.runId)
  assert.notEqual(next.confirmId, interruption.confirmId)
  assert.equal(finalMessage(events).parts.filter((p) => p.kind === 'plan').length, 1)
  assert.equal(
    events.some(
      (e) =>
        e.kind === 'message' &&
        e.message.parts.some(
          (p) => p.kind === 'tool_status' && p.toolName === 'handoff_to_planner',
        ),
    ),
    false,
  )
  assert.deepEqual(
    (await app.ports.sessions.getInterruptSnapshot(next.turn_uuid))?.continuation?.calls,
    draft.execution.calls,
  )
  assert.equal(await app.ports.sessions.getContinuationDraft(body.conversation_id), null)
  const resumed = parse(
    await (
      await app.api.request(
        '/v1/conversation',
        request({
          ...body,
          confirmId: next.confirmId,
          approved: true,
        }),
      )
    ).text(),
  )
  assert.equal(finalMessage(resumed).parts.filter((p) => p.kind === 'artifact').length, 3)
  assert.ok(
    finalMessage(resumed).parts.some(
      (p) => p.kind === 'plan' && p.steps.every((s) => s.status === 'completed'),
    ),
  )
  // 再次取消可更新 draft；明确新任务必须丢弃旧 draft。
  const again = parse(
    await (
      await app.api.request(
        '/v1/conversation',
        request({ conversation_id: body.conversation_id, prompt: '咖啡广告' }),
      )
    ).text(),
  )
  const card = again.find((e) => e.kind === 'system' && e.type === 'interrupt_approval')
  assert.ok(card && card.kind === 'system' && card.type === 'interrupt_approval')
  await (
    await app.api.request(
      '/v1/conversation',
      request({ ...body, confirmId: card.confirmId, approved: false }),
    )
  ).text()
  const fresh = parse(
    await (
      await app.api.request(
        '/v1/conversation',
        request({ conversation_id: body.conversation_id, prompt: '生成一张猫' }),
      )
    ).text(),
  )
  assert.equal(await app.ports.sessions.getContinuationDraft(body.conversation_id), null)
  assert.equal(finalMessage(fresh).parts.filter((p) => p.kind === 'artifact').length, 1)
  assert.equal(
    finalMessage(fresh).parts.some((p) => p.kind === 'plan'),
    true,
  )
})

test('续跑保留已完成步骤与 index；仅执行剩余工具且 Trace 不重复计费', async () => {
  const app = setup()
  const { body } = await initial(app)
  await (await app.api.request('/v1/conversation', request({ ...body, approved: false }))).text()
  const draft = await app.ports.sessions.getContinuationDraft(body.conversation_id)
  assert.ok(draft?.execution.plan)
  // 持久化边界夹具：首镜头已完成，后两步等待取消后的续跑。
  draft.execution.index = 1
  draft.execution.plan.steps[0].status = 'completed'
  draft.execution.plan.steps[1].status = 'cancelled'
  draft.execution.credits = 40
  await app.ports.sessions.saveContinuationDraft(body.conversation_id, draft)
  const events = parse(
    await (
      await app.api.request(
        '/v1/conversation',
        request({ conversation_id: body.conversation_id, prompt: '接着做！' }),
      )
    ).text(),
  )
  const approval = events.find((e) => e.kind === 'system' && e.type === 'interrupt_approval')
  assert.ok(approval && approval.kind === 'system' && approval.type === 'interrupt_approval')
  assert.equal(approval.estimateCredits, 80)
  const snapshot = await app.ports.sessions.getInterruptSnapshot(approval.turn_uuid)
  assert.equal(snapshot?.continuation?.index, 1)
  assert.equal(snapshot?.continuation?.plan?.steps[0].status, 'completed')
  const done = parse(
    await (
      await app.api.request(
        '/v1/conversation',
        request({ ...body, confirmId: approval.confirmId, approved: true }),
      )
    ).text(),
  )
  assert.equal(finalMessage(done).parts.filter((p) => p.kind === 'artifact').length, 2)
  assert.equal(await app.ports.queue.getByIdempotencyKey(draft.execution.calls[0].id), null)
  assert.equal(app.traces.get(approval.runId)?.turns[0].metrics.credits, 80)
})

test('继续意图是可测短句规则；无 draft 仍走普通需求', async () => {
  for (const text of [
    '继续',
    ' 接着做。 ',
    '接着',
    '继续执行!',
    '继续生成',
    '继续上次',
    'Resume',
  ]) {
    assert.equal(isContinueIntent(text), true)
  }
  for (const text of [
    '继续生成一张猫',
    '帮我做广告',
    '生成一张猫',
    '继续上次计划并增加五个不同的镜头和新的对白',
  ]) {
    assert.equal(isContinueIntent(text), false)
  }
  const app = setup()
  const response = await app.api.request('/v1/conversation', request({ prompt: '继续' }))
  assert.equal(response.status, 200)
  assert.equal(finalMessage(parse(await response.text())).status, 'completed')
})

test('计划确认处停止后继续仍恢复计划与游标', async () => {
  const app = setup()
  const { body, interruption } = await initial(app)
  await (
    await app.api.request(
      '/v1/stop_stream',
      request({ conversation_id: body.conversation_id, turn_uuid: interruption.turn_uuid }),
    )
  ).text()
  assert.ok(await app.ports.sessions.getContinuationDraft(body.conversation_id))
  const events = parse(
    await (
      await app.api.request(
        '/v1/conversation',
        request({ conversation_id: body.conversation_id, prompt: '继续' }),
      )
    ).text(),
  )
  assert.ok(finalMessage(events).parts.some((p) => p.kind === 'plan'))
  assert.ok(events.some((e) => e.kind === 'system' && e.type === 'interrupt_approval'))
})

test('S4 固定窗口限流与并发入场返回429，终态释放槽位', async () => {
  const app = setup({ rateOptions: { requests: 1, concurrency: 1 } })
  const user = randomUUID()
  const first = await app.service.prepare('猫', user)
  const denied = await app.api.request(
    '/v1/conversation',
    request({ prompt: '猫', conversation_id: user }),
  )
  assert.equal(denied.status, 429)
  assert.ok(denied.headers.get('Retry-After'))
  await app.service.run(first, async () => {})
  const limited = await app.api.request(
    '/v1/conversation',
    request({ prompt: '猫', conversation_id: user }),
  )
  assert.equal(limited.status, 429)
  const concurrencyApp = setup({ rateOptions: { concurrency: 1, requests: 100 } })
  const running = await concurrencyApp.service.prepare('猫', user)
  await assert.rejects(concurrencyApp.service.prepare('猫', user), /同时执行/)
  await concurrencyApp.service.run(running, async () => {})
  const next = await concurrencyApp.service.prepare('猫', user)
  await concurrencyApp.service.run(next, async () => {})
  const { MemoryRateLimiter } = await import('../src/adapters/memory/index.js')
  let now = 0
  const limiter = new MemoryRateLimiter({ requests: 2, windowMs: 10, clock: () => now })
  assert.equal((await limiter.allow('a', 2)).ok, true)
  assert.equal((await limiter.allow('a')).ok, false)
  assert.equal((await limiter.allow('b')).ok, true)
  now = 10
  assert.equal((await limiter.allow('a')).ok, true)
})

test('S4 异步队列并发幂等、取消、超时与背压', async () => {
  const { MemoryAsyncQueue } = await import('../src/adapters/memory/index.js')
  const queue = new MemoryAsyncQueue({ timeoutMs: 15, maxPending: 1 })
  let count = 0
  let release: (() => void) | undefined
  queue.register('slow', async () => {
    count++
    await new Promise<void>((resolve) => {
      release = resolve
    })
    return 'done'
  })
  const controller = new AbortController()
  const first = queue.enqueue({
    name: 'slow',
    payload: {},
    idempotencyKey: 'one',
    signal: controller.signal,
  })
  const duplicate = queue.enqueue({ name: 'slow', payload: {}, idempotencyKey: 'one' })
  assert.equal(count, 0)
  const settled = Promise.allSettled([first, duplicate])
  await new Promise<void>((resolve) => setImmediate(resolve))
  assert.equal(count, 1)
  await assert.rejects(
    queue.enqueue({ name: 'slow', payload: {}, idempotencyKey: 'two' }),
    /队列已满/,
  )
  controller.abort()
  // 不合作 handler 仍占容量，enqueue 必须等待实际结束再交回槽位。
  await assert.rejects(
    queue.enqueue({ name: 'slow', payload: {}, idempotencyKey: 'cancel-pending' }),
    /队列已满/,
  )
  release?.()
  await settled
  assert.equal((await queue.getByIdempotencyKey('one'))?.status, 'cancelled')
  await new Promise<void>((resolve) => setImmediate(resolve))
  assert.equal((await queue.getByIdempotencyKey('one'))?.status, 'cancelled')
  const timeout = queue.enqueue({ name: 'slow', payload: {}, idempotencyKey: 'timeout' })
  setTimeout(() => release?.(), 20)
  await assert.rejects(timeout, /任务超时/)
  assert.equal((await queue.getByIdempotencyKey('timeout'))?.status, 'failed')
})

test('review 回归：生成中停止后继续使用新执行 ID，原计划步骤 ID 保留', async () => {
  const app = setup({ toolDelayMs: 100 })
  const { body, interruption } = await initial(app)
  const snapshot = await app.ports.sessions.getInterruptSnapshot(interruption.turn_uuid)
  const oldCall = snapshot!.continuation!.calls[0]!.id
  const originalSteps = snapshot!.continuation!.plan!.steps.map((s) => s.id)
  const running = Promise.resolve(
    app.api.request('/v1/conversation', request({ ...body, approved: true })),
  ).then((r) => r.text())
  for (let i = 0; i < 100 && !(await app.ports.queue.getByIdempotencyKey(oldCall)); i++) {
    await new Promise<void>((resolve) => setTimeout(resolve, 1))
  }
  assert.equal((await app.ports.queue.getByIdempotencyKey(oldCall))?.status, 'running')
  await app.service.stop(body.conversation_id, interruption.turn_uuid)
  await running
  const events = parse(
    await (
      await app.api.request(
        '/v1/conversation',
        request({ conversation_id: body.conversation_id, prompt: '继续' }),
      )
    ).text(),
  )
  const card = events.find((e) => e.kind === 'system' && e.type === 'interrupt_approval')
  assert.ok(card && card.kind === 'system' && card.type === 'interrupt_approval')
  const restored = await app.ports.sessions.getInterruptSnapshot(card.turn_uuid)
  assert.notEqual(restored!.continuation!.calls[0]!.id, oldCall)
  assert.deepEqual(
    restored!.continuation!.plan!.steps.map((s) => s.id),
    originalSteps,
  )
  const done = parse(
    await (
      await app.api.request(
        '/v1/conversation',
        request({ ...body, confirmId: card.confirmId, approved: true }),
      )
    ).text(),
  )
  assert.equal(finalMessage(done).status, 'completed')
  assert.equal(finalMessage(done).parts.filter((p) => p.kind === 'artifact').length, 3)
})

test('review 回归：dequeue 不能抢走自动消费任务，不泄漏容量', async () => {
  const { MemoryAsyncQueue } = await import('../src/adapters/memory/index.js')
  const queue = new MemoryAsyncQueue({ maxPending: 1 })
  let count = 0
  queue.register('job', async () => ++count)
  const pending = queue.enqueue({ name: 'job', payload: {}, idempotencyKey: 'first' })
  assert.equal(await queue.dequeue(), null)
  assert.equal((await pending).status, 'completed')
  assert.equal(
    (await queue.enqueue({ name: 'job', payload: {}, idempotencyKey: 'second' })).status,
    'completed',
  )
  assert.equal(count, 2)
})

test('review 回归：过期窗口与任务收据回收，未过期幂等仍有效', async () => {
  const { MemoryRateLimiter, MemoryTaskQueue } = await import('../src/adapters/memory/index.js')
  let time = Date.now()
  const limiter = new MemoryRateLimiter({ maxKeys: 1, windowMs: 10, clock: () => time })
  assert.equal((await limiter.allow('first')).ok, true)
  assert.equal((await limiter.allow('second')).ok, false)
  time += 11
  assert.equal((await limiter.allow('second')).ok, true)
  const queue = new MemoryTaskQueue({ maxRecords: 1, retentionMs: 10, clock: () => time })
  let count = 0
  queue.register('job', async () => ++count)
  time = Date.now()
  const first = await queue.enqueue({ name: 'job', payload: {}, idempotencyKey: 'first' })
  assert.equal(
    (await queue.enqueue({ name: 'job', payload: {}, idempotencyKey: 'first' })).id,
    first.id,
  )
  await assert.rejects(
    queue.enqueue({ name: 'job', payload: {}, idempotencyKey: 'second' }),
    /幂等记录容量/,
  )
  time = Date.now() + 20
  assert.equal(await queue.getByIdempotencyKey('first'), null)
  assert.equal(
    (await queue.enqueue({ name: 'job', payload: {}, idempotencyKey: 'second' })).status,
    'completed',
  )
  assert.equal(count, 2)
})
