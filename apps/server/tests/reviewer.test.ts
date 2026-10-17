import assert from 'node:assert/strict'
import { test } from 'node:test'
import { mkdtemp, writeFile, rm } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { StubReviewerProvider, finalizeGeneration } from '../src/agent/reflection.js'
import { createReviewer, reviewerOptionsFromEnv } from '../src/adapters/reviewer/factory.js'
import { RealReviewer } from '../src/adapters/reviewer/real.js'
import { createApplication } from '../src/app/create-app.js'
import { MemoryTraceWriter } from '../src/adapters/memory/index.js'
import type { ReviewInput } from '../src/ports/index.js'
import type { LoggedEvent } from '../src/domain/model.js'
import { toolName } from '../src/tools/text2image.js'
const options = {
  api: 'https://review.example/v1/chat/completions',
  model: 'test-vlm',
  apiKey: 'unit-test-only',
  timeoutMs: 100,
}
function input(signal = new AbortController().signal): ReviewInput {
  return {
    artifact: { url: 'https://assets.example/good.png', fixtureId: 'good' },
    result: {
      artifact: {
        kind: 'artifact',
        id: 'image-1',
        type: 'image',
        url: 'https://assets.example/good.png',
        status: 'success',
      },
      credits: 60,
      metadata: { width: 560, height: 700, bytes: 100 },
    },
    prompt: '完整咖啡杯海报，4:5',
    taskType: 'image',
    rubricId: 'image-quality-v1',
    rubric: ['相关性', '明显残缺', '比例'],
    attempt: 2,
    context: { conversationId: 'conversation', turnId: 'turn', toolCallId: 'call' },
    signal,
  }
}
function completion(
  review = {
    score: 0.9,
    rubric: input().rubric.map((item) => ({ item, pass: true })),
    issues: [] as string[],
  },
) {
  return new Response(
    JSON.stringify({ choices: [{ message: { content: JSON.stringify(review) } }] }),
  )
}
test('工厂默认/非法 mode 用 stub，显式 real 用真实实现，env 配置不携带 key', async () => {
  assert.ok(createReviewer() instanceof StubReviewerProvider)
  assert.ok(createReviewer({ mode: 'invalid' }) instanceof StubReviewerProvider)
  assert.ok(createReviewer({ mode: 'real' }, {}) instanceof RealReviewer)
  const env = {
    REVIEWER_MODE: 'real',
    REAL_REVIEWER_API: options.api,
    REAL_REVIEWER_MODEL: options.model,
    REAL_REVIEWER_API_KEY: options.apiKey,
    REVIEW_RUBRIC: '{"image":["相关性","明显残缺","比例"]}',
  }
  const config = reviewerOptionsFromEnv(env)
  assert.ok(createReviewer(config.reviewer, env) instanceof RealReviewer)
  assert.equal(JSON.stringify(config).includes(options.apiKey), false)
  assert.deepEqual(config.rubric?.image, input().rubric)
  assert.throws(() => reviewerOptionsFromEnv({ REVIEW_RUBRIC: '{"image":[]}' }))
  assert.throws(() => new RealReviewer({ timeoutMs: 15001 }))
  assert.equal((await createReviewer({ mode: 'bad' }).review(input())).reviewer, 'stub')
})
test('真实 HTTP 成功发送图片、prompt、rubric 和 Trace 上下文；模型不能伪造身份/attempt', async () => {
  const http: typeof fetch = async (url, init) => {
    assert.equal(String(url), options.api)
    assert.equal(
      (init?.headers as Record<string, string>).Authorization,
      `Bearer ${options.apiKey}`,
    )
    const request = JSON.parse(String(init?.body)) as {
      model: string
      messages: { content: { type: string; text?: string; image_url?: { url: string } }[] }[]
    }
    assert.equal(request.model, options.model)
    const contents = request.messages[1]!.content
    assert.deepEqual(JSON.parse(contents[0]!.text!).context, input().context)
    assert.equal(JSON.parse(contents[0]!.text!).prompt, input().prompt)
    assert.equal(contents[1]!.image_url?.url, input().artifact.url)
    return new Response(
      JSON.stringify({
        choices: [
          {
            message: {
              content: JSON.stringify({
                score: 0.9,
                rubric: input().rubric.map((item) => ({ item, pass: true })),
                issues: [],
                attempt: 99,
                reviewer: 'stub',
              }),
            },
          },
        ],
      }),
    )
  }
  const review = await new RealReviewer(options, http).review(input())
  assert.equal(review.score, 0.9)
  assert.equal(review.reviewer, options.model)
  assert.equal(review.attempt, 2)
})
test('超时覆盖 fetch 与响应读取，即使 HTTP 替身不响应 AbortSignal 也会退出', async () => {
  for (const http of [
    (async () => new Promise<Response>(() => {})) as typeof fetch,
    (async () => new Response(new ReadableStream({ start() {} }))) as typeof fetch,
  ]) {
    const review = await new RealReviewer({ ...options, timeoutMs: 15 }, http).review(input())
    assert.equal(review.score, 0)
    assert.match(review.issues[0]!, /超过 15ms/)
    assert.ok(review.rubric.every((item) => !item.pass))
  }
})
for (const [name, response] of [
  ['坏 envelope JSON', () => new Response('not json')],
  [
    '坏审核 JSON',
    () => new Response(JSON.stringify({ choices: [{ message: { content: '{broken' } }] })),
  ],
  ['空响应', () => new Response('')],
  ['空 choices', () => new Response(JSON.stringify({ choices: [] }))],
  [
    '分数非法',
    () =>
      completion({
        score: 2,
        rubric: input().rubric.map((item) => ({ item, pass: true })),
        issues: [],
      }),
  ],
  [
    '关键项缺失',
    () => completion({ score: 1, rubric: [{ item: '其他', pass: true }], issues: [] }),
  ],
  ['HTTP 拒绝', () => new Response(options.apiKey, { status: 401 })],
] as const) {
  test(`${name} 记 issues 并进入有限 retry/escalate，不回显密钥`, async () => {
    const reviewer = new RealReviewer(options, async () => response())
    const source = input()
    for (const attempt of [1, 3]) {
      const final = await finalizeGeneration(
        source.result,
        attempt,
        reviewer,
        {},
        source.signal,
        source.prompt,
        source.context,
      )
      assert.equal(final.outcome, attempt === 1 ? 'retried' : 'escalated')
      assert.ok(final.evidence?.review?.issues.length)
      assert.equal(final.evidence?.review?.reviewer, options.model)
      assert.equal(JSON.stringify(final).includes(options.apiKey), false)
    }
  })
}
test('无 key 不调用 HTTP；用户取消传播，不能被吞成质量失败', async () => {
  let calls = 0
  const missing = new RealReviewer({ ...options, apiKey: undefined }, async () => {
    calls++
    return completion()
  })
  assert.equal((await missing.review(input())).score, 0)
  assert.equal(calls, 0)
  const controller = new AbortController()
  const review = new RealReviewer(options, async () => new Promise<Response>(() => {})).review(
    input(controller.signal),
  )
  controller.abort(new Error('用户停止'))
  await assert.rejects(review, /用户停止/)
})
test('本地图片转 data URL，路径与夹具标识不上传', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'aivideo-reviewer-'))
  try {
    const path = join(directory, 'image.png')
    await writeFile(
      path,
      Buffer.from(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lXcAAAAASUVORK5CYII=',
        'base64',
      ),
    )
    const source = input()
    source.artifact.localPath = path
    const reviewer = new RealReviewer(options, async (_url, init) => {
      const body = String(init?.body)
      assert.match(body, /data:image\/png;base64,/)
      assert.equal(body.includes(path), false)
      assert.equal(body.includes('fixtureId'), false)
      return completion()
    })
    assert.equal((await reviewer.review(source)).score, 0.9)
  } finally {
    await rm(directory, { recursive: true, force: true })
  }
})
function parse(text: string): LoggedEvent[] {
  return text.split('\n\n').flatMap((frame) => {
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
test('real composition root 写入同一 Trace，审核失败重试重新中断积分', async () => {
  const originalHttp = globalThis.fetch
  const originalKey = process.env.REAL_REVIEWER_API_KEY
  globalThis.fetch = async () => new Response('坏响应')
  process.env.REAL_REVIEWER_API_KEY = options.apiKey
  let generations = 0
  try {
    const traces = new MemoryTraceWriter()
    const app = createApplication({
      reviewer: { mode: 'real', real: { api: options.api, model: options.model } },
      ports: { traces },
      llmDelayMs: 0,
      streamCharDelayMs: 0,
      tools: [
        {
          name: toolName,
          estimateCredits: 60,
          async execute() {
            generations++
            return input().result
          },
        },
      ],
    })
    const first = parse(
      await (
        await app.api.request('/v1/conversation', post({ prompt: '生成一张咖啡海报' }))
      ).text(),
    )
    const confirm = first.find(
      (event) => event.kind === 'system' && event.type === 'interrupt_approval',
    )!
    assert.ok(confirm.kind === 'system' && confirm.type === 'interrupt_approval')
    const reviewed = parse(
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
    assert.equal(generations, 1)
    const next = reviewed.find(
      (event) => event.kind === 'system' && event.type === 'interrupt_approval',
    )!
    assert.ok(next.kind === 'system' && next.type === 'interrupt_approval')
    assert.equal(next.estimateCredits, 60)
    const call = traces
      .get(first[0]!.runId)!
      .turns[0]!.toolCalls.find((call) => call.name === toolName)!
    assert.equal(call.evidence?.review?.reviewer, options.model)
    assert.equal(call.evidence?.review?.score, 0)
    assert.equal(call.reviewOutcome, 'retried')
    await (
      await app.api.request(
        '/v1/conversation',
        post({
          action: 'resume_interrupt',
          conversation_id: next.conversation_id,
          confirmId: next.confirmId,
          approved: false,
        }),
      )
    ).text()
    assert.equal(generations, 1)
  } finally {
    globalThis.fetch = originalHttp
    if (originalKey === undefined) {
      delete process.env.REAL_REVIEWER_API_KEY
    } else {
      process.env.REAL_REVIEWER_API_KEY = originalKey
    }
  }
})

test('REVIEWER_MODE=real 好图直接接受并在 Trace 写真实模型', async () => {
  const originalHttp = globalThis.fetch
  const originalKey = process.env.REAL_REVIEWER_API_KEY
  globalThis.fetch = async () => completion()
  process.env.REAL_REVIEWER_API_KEY = options.apiKey
  try {
    const config = reviewerOptionsFromEnv({
      REVIEWER_MODE: 'real',
      REAL_REVIEWER_API: options.api,
      REAL_REVIEWER_MODEL: options.model,
    })
    const traces = new MemoryTraceWriter()
    const app = createApplication({
      ...config,
      ports: { traces },
      llmDelayMs: 0,
      streamCharDelayMs: 0,
      tools: [
        {
          name: toolName,
          estimateCredits: 40,
          async execute() {
            return { ...input().result, credits: 40 }
          },
        },
      ],
    })
    const events = parse(
      await (
        await app.api.request('/v1/conversation', post({ prompt: '生成一张咖啡海报' }))
      ).text(),
    )
    const call = traces
      .get(events[0]!.runId)!
      .turns[0]!.toolCalls.find((item) => item.name === toolName)!
    assert.equal(call.reviewOutcome, 'accepted')
    assert.equal(call.reviewAttempts, 0)
    assert.equal(call.evidence?.review?.reviewer, options.model)
  } finally {
    globalThis.fetch = originalHttp
    if (originalKey === undefined) {
      delete process.env.REAL_REVIEWER_API_KEY
    } else {
      process.env.REAL_REVIEWER_API_KEY = originalKey
    }
  }
})
