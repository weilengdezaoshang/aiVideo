import assert from 'node:assert/strict'
import { test } from 'node:test'
import { finalizeGeneration, StubReviewerProvider } from '../src/agent/reflection.js'
import type { GeneratedToolResult, LoggedEvent } from '../src/domain/model.js'
import { createApplication } from '../src/app/create-app.js'
import { MemoryTraceWriter } from '../src/adapters/memory/index.js'
import { toolName } from '../src/tools/text2image.js'

function fixture(pass: boolean): GeneratedToolResult {
  return {
    artifact: { kind: 'artifact', id: 'a', type: 'image', url: '/mock.svg', status: 'success' },
    credits: 40,
    metadata: { width: 560, height: 700, bytes: 100, reviewPass: pass },
  }
}
for (const [pass, attempt, expected] of [
  [true, 1, 'accepted'],
  [false, 1, 'retried'],
  [false, 3, 'escalated'],
] as const) {
  test(`finalizeGeneration ${expected}`, async () => {
    const result = fixture(pass)
    const final = await finalizeGeneration(
      result,
      attempt,
      new StubReviewerProvider(),
      {},
      new AbortController().signal,
    )
    assert.equal(final.outcome, expected)
    assert.equal(result.evidence?.review?.attempt, attempt)
    assert.equal(
      final.evidence?.checks.every((check) => check.pass),
      true,
    )
    if (!pass) {
      assert.ok(final.evidence?.review?.issues.length)
    }
  })
}
test('硬检查阻止高分通过，关闭审核不调用 port', async () => {
  const result = fixture(true)
  result.metadata!.bytes = 0
  const final = await finalizeGeneration(
    result,
    3,
    new StubReviewerProvider(),
    {},
    new AbortController().signal,
  )
  assert.equal(final.outcome, 'escalated')
  assert.ok(final.evidence?.review?.issues.includes('硬检查未通过：non_empty'))
  const disabled = await finalizeGeneration(
    result,
    1,
    {
      review: async () => {
        throw new Error('不应审核')
      },
    },
    { reviewEnabled: false },
    new AbortController().signal,
  )
  assert.equal(disabled.outcome, 'accepted')
  assert.equal(disabled.evidence, undefined)
})
function events(body: string): LoggedEvent[] {
  return body.split('\n\n').flatMap((frame) => {
    const line = frame.split('\n').find((item) => item.startsWith('data:'))
    return line ? [JSON.parse(line.slice(5)) as LoggedEvent] : []
  })
}
function post(body: object) {
  return {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }
}
test('重试使用新幂等键并重新积分确认，取消后继续不丢次数', async () => {
  const traces = new MemoryTraceWriter()
  let runs = 0
  const app = createApplication({
    llmDelayMs: 0,
    streamCharDelayMs: 0,
    creditThreshold: 50,
    ports: { traces },
    tools: [
      {
        name: toolName,
        estimateCredits: 60,
        async execute() {
          runs++
          return { ...fixture(runs > 1), credits: 60 }
        },
      },
    ],
  })
  const first = events(
    await (await app.api.request('/v1/conversation', post({ prompt: '生成一张咖啡海报' }))).text(),
  )
  const confirm = first.find(
    (event) => event.kind === 'system' && event.type === 'interrupt_approval',
  )!
  assert.ok(confirm.kind === 'system' && confirm.type === 'interrupt_approval')
  const second = events(
    await (
      await app.api.request(
        '/v1/conversation',
        post({
          action: 'resume_interrupt',
          conversation_id: confirm.conversation_id,
          confirmId: confirm.confirmId,
          approved: true,
        }),
      )
    ).text(),
  )
  assert.equal(runs, 1)
  const retry = second.find(
    (event) => event.kind === 'system' && event.type === 'interrupt_approval',
  )!
  assert.ok(retry.kind === 'system' && retry.type === 'interrupt_approval')
  assert.equal(retry.estimateCredits, 60)
  await (
    await app.api.request(
      '/v1/conversation',
      post({
        action: 'resume_interrupt',
        conversation_id: retry.conversation_id,
        confirmId: retry.confirmId,
        approved: false,
      }),
    )
  ).text()
  assert.equal(runs, 1)
  const continued = events(
    await (
      await app.api.request(
        '/v1/conversation',
        post({ conversation_id: retry.conversation_id, prompt: '继续' }),
      )
    ).text(),
  )
  const next = continued.find(
    (event) => event.kind === 'system' && event.type === 'interrupt_approval',
  )!
  assert.ok(next.kind === 'system' && next.type === 'interrupt_approval')
  const done = events(
    await (
      await app.api.request(
        '/v1/conversation',
        post({
          action: 'resume_interrupt',
          conversation_id: next.conversation_id,
          confirmId: next.confirmId,
          approved: true,
        }),
      )
    ).text(),
  )
  assert.equal(runs, 2)
  const trace = traces.get(done[0]!.runId)!
  const call = trace.turns[0]!.toolCalls.find((call) => call.name === toolName)!
  assert.equal(call.reviewOutcome, 'accepted')
  assert.equal(call.reviewAttempts, 1)
  assert.equal((await app.ports.queue.getByIdempotencyKey(`${call.id}:2`))?.status, 'completed')
})
test('持续低分只执行三次，输出原因和失败作品卡', async () => {
  let runs = 0
  const app = createApplication({
    llmDelayMs: 0,
    streamCharDelayMs: 0,
    tools: [
      {
        name: toolName,
        async execute() {
          runs++
          return fixture(false)
        },
      },
    ],
  })
  const result = events(
    await (await app.api.request('/v1/conversation', post({ prompt: '生成一张咖啡海报' }))).text(),
  )
  assert.equal(runs, 3)
  const last = [...result].reverse().find((event) => event.kind === 'message')!
  assert.ok(last.kind === 'message')
  assert.ok(
    last.message.parts.some(
      (part) =>
        part.kind === 'artifact' &&
        part.status === 'failed' &&
        part.qualityIssue === '未通过质量检查',
    ),
  )
  assert.ok(
    last.message.parts.some(
      (part) => part.kind === 'text' && part.text.includes('画面不符合创作要求'),
    ),
  )
})

test('关闭重试计费后只计首轮；自定义关键项缺失不能接受', async () => {
  const traces = new MemoryTraceWriter()
  let runs = 0
  const app = createApplication({
    llmDelayMs: 0,
    streamCharDelayMs: 0,
    retryCredits: false,
    ports: { traces },
    tools: [
      {
        name: toolName,
        async execute() {
          runs++
          return fixture(runs > 1)
        },
      },
    ],
  })
  const result = events(
    await (await app.api.request('/v1/conversation', post({ prompt: '生成一张咖啡海报' }))).text(),
  )
  assert.equal(runs, 2)
  assert.equal(traces.get(result[0]!.runId)!.turns[0]!.metrics.credits, 40)
  const final = await finalizeGeneration(
    fixture(true),
    1,
    {
      async review(input) {
        return {
          score: 1,
          rubric: [{ item: '其他项目', pass: true }],
          issues: [],
          attempt: input.attempt,
          reviewer: 'test',
        }
      },
    },
    { rubric: { image: ['品牌准确'] } },
    new AbortController().signal,
  )
  assert.equal(final.outcome, 'retried')
})

test('生成重试途中停止后继续，换执行 ID 仍保留上限', async () => {
  const traces = new MemoryTraceWriter()
  const app = createApplication({
    creditThreshold: 500,
    llmDelayMs: 0,
    streamCharDelayMs: 0,
    ports: { traces },
    tools: [
      {
        name: toolName,
        async execute(input, signal) {
          if (input.attempt === 2) {
            await new Promise<void>((resolve, reject) => {
              const timer = setTimeout(resolve, 200)
              signal.addEventListener(
                'abort',
                () => {
                  clearTimeout(timer)
                  reject(signal.reason)
                },
                { once: true },
              )
            })
          }
          return fixture(false)
        },
      },
    ],
  })
  const conversation = await app.ports.sessions.createConversation()
  const running = Promise.resolve(
    app.api.request(
      '/v1/conversation',
      post({ conversation_id: conversation.id, prompt: '做一条咖啡广告，3个镜头' }),
    ),
  ).then((response) => response.text())
  let turnId = ''
  for (let i = 0; i < 100; i++) {
    const record = (await app.ports.sessions.listTurns(conversation.id))[0]
    if (record) {
      turnId = record.id
      const turn = await app.ports.sessions.getRuntimeTurn(conversation.id, turnId)
      const call = turn?.execution?.calls[0]
      if (
        call &&
        (await app.ports.queue.getByIdempotencyKey(`${call.id}:2`))?.status === 'running'
      ) {
        break
      }
    }
    await new Promise<void>((resolve) => setTimeout(resolve, 1))
  }
  assert.ok(turnId)
  await app.service.stop(conversation.id, turnId)
  await running
  const draft = await app.ports.sessions.getContinuationDraft(conversation.id)
  const originalId = draft!.execution.calls[0]!.id
  assert.equal(draft!.execution.generationAttempts?.[originalId], 2)
  const continued = events(
    await (
      await app.api.request(
        '/v1/conversation',
        post({ conversation_id: conversation.id, prompt: '继续' }),
      )
    ).text(),
  )
  const trace = traces.get(continued[0]!.runId)!
  const call = trace.turns[0]!.toolCalls.find((item) => item.name === toolName)!
  assert.notEqual(call.id, originalId)
  assert.equal(call.reviewAttempts, 2)
  assert.equal(call.reviewOutcome, 'escalated')
  assert.equal(call.reviewHistory?.length, 2)
})
