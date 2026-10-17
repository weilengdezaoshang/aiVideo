import { StubReviewerProvider } from '../agent/reflection.js'
import { randomUUID } from 'node:crypto'
import { mkdir, readFile, readdir, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'
import { MemoryAsyncQueue, MemorySessionStore, StubLlmProvider } from '../adapters/memory/index.js'
import { createApplication } from '../app/create-app.js'
import { assertMemoryDrivers } from '../app/drivers.js'
import type { LoggedEvent } from '../domain/model.js'

interface Step {
  action: 'send' | 'resume' | 'stop' | 'invalid' | 'resume_unknown' | 'stop_unknown' | 'queue'
  prompt?: string
  approved?: boolean
  body?: unknown
  when?: string
  probe?: string
}

interface CaseFile {
  caseId: string
  steps: Step[]
  mocks?: {
    imageProvider?: string
    toolDelayMs?: number
    imageCredits?: number
    creditThreshold?: number
    reviewEnabled?: boolean
    reviewFixture?: 'good' | 'bad_once' | 'bad'
    maxReviewRetry?: number
  }
}

interface Measured {
  draftPresent: boolean
  draftIndex: number | null
  draftStepIds: string[]
  draftStatuses: string[]
  interruptIndex: number | null
  interruptStepIds: string[]
  interruptStatuses: string[]
}

interface TurnObservation {
  action: string
  prompt?: string
  approved?: boolean
  httpStatus: number
  errorBody: { error?: string } | null
  events: LoggedEvent[]
  tracePath: string | null
  measured: Measured
}

function arg(name: string): string {
  const index = process.argv.indexOf(name)
  const value = index >= 0 ? process.argv[index + 1] : undefined
  if (!value) {
    throw new Error(`缺少参数 ${name}`)
  }
  return value
}

function parseSse(text: string): LoggedEvent[] {
  return text.split('\n\n').flatMap((frame) => {
    const data = frame.split('\n').find((line) => line.startsWith('data:'))
    if (!data) {
      return []
    }
    return [JSON.parse(data.slice(5)) as LoggedEvent]
  })
}

function post(body: unknown) {
  return {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Origin: 'http://localhost:5173' },
    body: JSON.stringify(body),
  }
}

async function tracePath(directory: string, runId: string | undefined): Promise<string | null> {
  if (!runId) {
    return null
  }
  const path = join(directory, `${runId}.json`)
  const names = await readdir(directory).catch((): string[] => [])
  return names.includes(`${runId}.json`) ? path : null
}

async function main() {
  const casePath = arg('--case')
  const outDir = arg('--out')
  const spec = JSON.parse(await readFile(casePath, 'utf8')) as CaseFile
  if (spec.mocks?.imageProvider && spec.mocks.imageProvider !== 'stub') {
    throw new Error('agent_flow 评测只允许 mocks.imageProvider=stub')
  }
  await mkdir(outDir, { recursive: true })
  const traceDirectory = join(outDir, 'traces')
  await mkdir(traceDirectory, { recursive: true })
  const queueOnly = spec.steps.every((step) => step.action === 'queue')
  const observation = queueOnly
    ? await runQueueCase(spec.caseId, spec.steps, traceDirectory)
    : await runConversation(spec, traceDirectory)
  await writeFile(join(outDir, 'observation.json'), JSON.stringify(observation, null, 2))
}

async function runConversation(spec: CaseFile, traceDirectory: string) {
  // 故障仅在离线评测进程注入，不进入 app/main 或生产适配器。
  const fault = process.env.AGENT_EVAL_FAULT
  if (fault && !['f1', 'f2', 'f3', 'f4'].includes(fault)) {
    throw new Error(`未知评测故障 ${fault}`)
  }
  const sessions = new MemorySessionStore()
  const llm = new StubLlmProvider(0)
  const mocks = spec.mocks ?? {}
  const app = createApplication({
    traceDirectory,
    ports: {
      sessions,
      reviewer: {
        async review(input) {
          const stub = new StubReviewerProvider()
          const reviewPass =
            mocks.reviewFixture !== 'bad' &&
            (mocks.reviewFixture !== 'bad_once' || input.attempt > 1)
          const review = await stub.review({
            ...input,
            result: {
              ...input.result,
              metadata: { width: 560, height: 700, bytes: 100, reviewPass },
            },
          })
          // F4：故障适配器忽略低分和失败关键项，Host 不应再重试。
          return fault === 'f4'
            ? {
                ...review,
                score: 1,
                rubric: review.rubric.map((item) => ({ ...item, pass: true })),
                issues: [],
                reviewer: 'fault-f4-ignore-quality',
              }
            : review
        },
      },
      llm: {
        async act(input) {
          const result = await llm.act(input)
          if (
            fault === 'f1' &&
            result.toolCalls?.some((call) => call.name === 'handoff_to_planner')
          ) {
            return { text: '故障替身：未将复杂任务交给策划。' }
          }
          return result
        },
      },
    },
    reviewEnabled: mocks.reviewEnabled,
    maxReviewRetry: mocks.maxReviewRetry,
    llmDelayMs: 0,
    toolDelayMs: mocks.toolDelayMs ?? 0,
    imageCredits: mocks.imageCredits ?? 40,
    creditThreshold: mocks.creditThreshold ?? 50,
    queueOptions: { async: true, timeoutMs: 30000, maxPending: 32 },
  })
  let conversationId: string | undefined
  let confirmId: string | undefined
  let turnId: string | undefined
  const turns: TurnObservation[] = []
  const traces: string[] = []
  const remember = async (turn: TurnObservation) => {
    if (turn.tracePath && !traces.includes(turn.tracePath)) {
      traces.push(turn.tracePath)
    }
    const event = [...turn.events].reverse().find((item) => item.conversation_id && item.turn_uuid)
    if (event) {
      conversationId = event.conversation_id
      turnId = event.turn_uuid
    }
    const approval = [...turn.events]
      .reverse()
      .find((item) => item.kind === 'system' && item.type === 'interrupt_approval')
    if (approval && approval.kind === 'system' && approval.type === 'interrupt_approval') {
      confirmId = approval.confirmId
      conversationId = approval.conversation_id
      turnId = approval.turn_uuid
    }
    turns.push(turn)
  }
  for (let index = 0; index < spec.steps.length; index += 1) {
    const step = spec.steps[index]!
    const next = spec.steps[index + 1]
    if (step.action === 'resume' && next?.action === 'stop' && next.when === 'generating') {
      if (!conversationId || !confirmId || !turnId) {
        throw new Error('停止生成前没有可恢复的确认')
      }
      const consumed = (async () => {
        const response = await app.api.request(
          '/v1/conversation',
          post({
            action: 'resume_interrupt',
            conversation_id: conversationId,
            confirmId,
            approved: fault === 'f2' && step.approved === false ? true : step.approved,
          }),
        )
        return { status: response.status, text: await response.text() }
      })()
      let completed = 0
      for (let attempt = 0; attempt < 200; attempt += 1) {
        const runtime = await app.ports.sessions.getRuntimeTurn(conversationId, turnId)
        completed =
          runtime?.execution?.plan?.steps.filter((item) => item.status === 'completed').length ?? 0
        if (completed >= 1) {
          break
        }
        await sleep(10)
      }
      await app.service.stop(conversationId, turnId)
      const body = await consumed
      await remember(
        await materializeTurn(
          app,
          body.status,
          body.text,
          traceDirectory,
          'resume',
          step.prompt,
          step.approved,
        ),
      )
      await remember({
        action: 'stop',
        httpStatus: 200,
        errorBody: null,
        events: [],
        tracePath: null,
        measured: await measure(app, conversationId),
      })
      index += 1
      continue
    }
    if (step.action === 'send') {
      if (fault === 'f3' && conversationId && step.prompt === '继续') {
        await sessions.clearContinuationDraft(conversationId)
      }
      const response = await app.api.request(
        '/v1/conversation',
        post({
          prompt: step.prompt,
          ...(conversationId ? { conversation_id: conversationId } : {}),
        }),
      )
      await remember(await readTurn(app, response, traceDirectory, 'send', step.prompt))
      continue
    }
    if (step.action === 'resume') {
      if (!conversationId || !confirmId) {
        throw new Error('resume 前没有确认卡')
      }
      const response = await app.api.request(
        '/v1/conversation',
        post({
          action: 'resume_interrupt',
          conversation_id: conversationId,
          confirmId,
          approved: fault === 'f2' && step.approved === false ? true : step.approved,
        }),
      )
      await remember(
        await readTurn(app, response, traceDirectory, 'resume', undefined, step.approved),
      )
      continue
    }
    if (step.action === 'invalid') {
      const response = await app.api.request('/v1/conversation', post(step.body ?? {}))
      await remember(await readTurn(app, response, traceDirectory, 'invalid'))
      continue
    }
    if (step.action === 'resume_unknown') {
      const response = await app.api.request(
        '/v1/conversation',
        post({
          action: 'resume_interrupt',
          conversation_id: randomUUID(),
          confirmId: randomUUID(),
          approved: false,
        }),
      )
      await remember(await readTurn(app, response, traceDirectory, 'resume_unknown'))
      continue
    }
    if (step.action === 'stop_unknown') {
      const response = await app.api.request(
        '/v1/stop_stream',
        post({ conversation_id: randomUUID(), turn_uuid: randomUUID() }),
      )
      await remember(await readTurn(app, response, traceDirectory, 'stop_unknown'))
      continue
    }
    if (step.action === 'stop') {
      if (!conversationId || !turnId) {
        throw new Error('stop 前没有轮次')
      }
      const stopped = await app.service.stop(conversationId, turnId)
      await remember({
        action: 'stop',
        httpStatus: stopped ? 200 : 404,
        errorBody: stopped ? null : { error: '会话轮次不存在' },
        events: [],
        tracePath: null,
        measured: await measure(app, conversationId),
      })
      continue
    }
    throw new Error(`对话用例不支持的 action:${step.action}`)
  }
  return { caseId: spec.caseId, turns, traces, queue: null }
}

async function readTurn(
  app: ReturnType<typeof createApplication>,
  response: Response,
  traceDirectory: string,
  action: string,
  prompt?: string,
  approved?: boolean,
): Promise<TurnObservation> {
  return materializeTurn(
    app,
    response.status,
    await response.text(),
    traceDirectory,
    action,
    prompt,
    approved,
  )
}

async function materializeTurn(
  app: ReturnType<typeof createApplication>,
  httpStatus: number,
  text: string,
  traceDirectory: string,
  action: string,
  prompt?: string,
  approved?: boolean,
): Promise<TurnObservation> {
  if (httpStatus !== 200) {
    let errorBody: { error?: string } | null = null
    try {
      errorBody = JSON.parse(text) as { error?: string }
    } catch {
      errorBody = { error: text }
    }
    return {
      action,
      prompt,
      approved,
      httpStatus,
      errorBody,
      events: [],
      tracePath: null,
      measured: emptyMeasured(),
    }
  }
  const events = parseSse(text)
  const runId = events.find((event) => event.runId)?.runId
  const conversationId = events.find((event) => event.conversation_id)?.conversation_id
  const path = await tracePath(traceDirectory, runId)
  return {
    action,
    prompt,
    approved,
    httpStatus,
    errorBody: null,
    events,
    tracePath: path,
    measured: conversationId ? await measure(app, conversationId) : emptyMeasured(),
  }
}

function emptyMeasured(): Measured {
  return {
    draftPresent: false,
    draftIndex: null,
    draftStepIds: [],
    draftStatuses: [],
    interruptIndex: null,
    interruptStepIds: [],
    interruptStatuses: [],
  }
}

async function measure(
  app: ReturnType<typeof createApplication>,
  conversationId: string,
): Promise<Measured> {
  const draft = await app.ports.sessions.getContinuationDraft(conversationId)
  const turns = await app.ports.sessions.listTurns(conversationId)
  let interruptIndex: number | null = null
  let interruptStepIds: string[] = []
  let interruptStatuses: string[] = []
  for (const turn of turns) {
    const snapshot = await app.ports.sessions.getInterruptSnapshot(turn.id)
    const plan = snapshot?.continuation?.plan
    if (snapshot?.continuation) {
      interruptIndex = snapshot.continuation.index
      interruptStepIds = plan?.steps.map((step) => step.id) ?? []
      interruptStatuses = plan?.steps.map((step) => step.status) ?? []
    }
  }
  return {
    draftPresent: Boolean(draft),
    draftIndex: draft?.execution.index ?? null,
    draftStepIds: draft?.execution.plan?.steps.map((step) => step.id) ?? [],
    draftStatuses: draft?.execution.plan?.steps.map((step) => step.status) ?? [],
    interruptIndex,
    interruptStepIds,
    interruptStatuses,
  }
}

async function runQueueCase(caseId: string, steps: Step[], traceDirectory: string) {
  const queue = await runQueue(steps, traceDirectory)
  const names = await readdir(traceDirectory).catch(() => [])
  const traces = names
    .filter((name) => name.endsWith('.json'))
    .map((name) => join(traceDirectory, name))
  return { caseId, turns: [], traces, queue }
}

async function runQueue(steps: Step[], traceDirectory: string) {
  const probe = steps[0]?.probe
  if (steps.length !== 1 || !probe) {
    throw new Error('队列评测每条用例只做一种 probe')
  }
  if (probe === 'rate_limit') {
    return rateLimit(traceDirectory)
  }
  if (probe === 'concurrency') {
    return concurrency(traceDirectory)
  }
  if (probe === 'idempotency') {
    return idempotency()
  }
  if (probe === 'cancel') {
    return cancelJob()
  }
  if (probe === 'timeout') {
    return timeoutJob()
  }
  if (probe === 'backpressure') {
    return backpressure()
  }
  if (probe === 'expiry') {
    return expiry()
  }
  if (probe === 'bullmq') {
    return bullmq()
  }
  throw new Error(`未知 probe:${probe}`)
}

async function rateLimit(traceDirectory: string) {
  const { api } = createApplication({
    traceDirectory,
    llmDelayMs: 0,
    toolDelayMs: 0,
    queueOptions: { async: true, timeoutMs: 5000 },
    rateOptions: { requests: 1, windowMs: 60000, concurrency: 5 },
  })
  const first = await api.request('/v1/conversation', post({ prompt: '生成一张咖啡海报' }))
  await first.text()
  const second = await api.request('/v1/conversation', post({ prompt: '生成一张咖啡海报' }))
  const body = (await second.json()) as { retryAfterMs?: number; error?: string }
  return {
    rateLimitStatus: second.status,
    retryAfter: second.headers.get('retry-after'),
    retryAfterMs: body.retryAfterMs ?? null,
    firstStatus: first.status,
  }
}

async function concurrency(traceDirectory: string) {
  const conversationId = randomUUID()
  const { api, ports } = createApplication({
    traceDirectory,
    llmDelayMs: 0,
    toolDelayMs: 1000,
    queueOptions: { async: true, timeoutMs: 5000 },
    rateOptions: { requests: 100, windowMs: 60000, concurrency: 1 },
  })
  const first = api.request(
    '/v1/conversation',
    post({ prompt: '生成一张咖啡海报', conversation_id: conversationId }),
  )
  let admitted = false
  for (let attempt = 0; attempt < 50; attempt += 1) {
    const turns = await ports.sessions.listTurns(conversationId)
    if (turns.some((turn) => turn.status === 'running')) {
      admitted = true
      break
    }
    await sleep(5)
  }
  const second = await api.request(
    '/v1/conversation',
    post({ prompt: '再来一张', conversation_id: conversationId }),
  )
  const status = second.status
  await second.text().catch(() => undefined)
  await (await first).text()
  return { concurrencyStatus: admitted ? status : 0 }
}

async function idempotency() {
  const queue = new MemoryAsyncQueue({ timeoutMs: 5000 })
  let handlerRuns = 0
  queue.register('job', async () => {
    handlerRuns += 1
    await sleep(20)
    return { ok: true }
  })
  const first = queue.enqueue({ name: 'job', payload: {}, idempotencyKey: 'same' })
  const second = queue.enqueue({ name: 'job', payload: {}, idempotencyKey: 'same' })
  const [left, right] = await Promise.all([first, second])
  return { handlerRuns, jobIds: [left.id, right.id] }
}

async function cancelJob() {
  const queue = new MemoryAsyncQueue({ timeoutMs: 5000 })
  queue.register('slow', async (_payload, ctx) => {
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(resolve, 10000)
      const abort = () => {
        clearTimeout(timer)
        reject(ctx.signal?.reason ?? new Error('任务已取消'))
      }
      if (ctx.signal?.aborted) {
        abort()
        return
      }
      ctx.signal?.addEventListener('abort', abort, { once: true })
    })
  })
  const controller = new AbortController()
  const pending = queue.enqueue({
    name: 'slow',
    payload: {},
    idempotencyKey: 'cancel-me',
    signal: controller.signal,
  })
  await sleep(15)
  controller.abort()
  await pending.catch(() => undefined)
  const job = await queue.getByIdempotencyKey('cancel-me')
  return { cancelStatus: job?.status ?? '' }
}

async function timeoutJob() {
  const queue = new MemoryAsyncQueue({ timeoutMs: 30 })
  queue.register('slow', async () => {
    await sleep(200)
    return { ok: true }
  })
  await queue
    .enqueue({ name: 'slow', payload: {}, idempotencyKey: 'too-slow' })
    .catch(() => undefined)
  const job = await queue.getByIdempotencyKey('too-slow')
  return { timeoutStatus: job?.status ?? '' }
}

async function backpressure() {
  const queue = new MemoryAsyncQueue({ maxPending: 1, timeoutMs: 5000 })
  let release: (() => void) | undefined
  queue.register(
    'slow',
    () =>
      new Promise((resolve) => {
        release = () => resolve({ ok: true })
      }),
  )
  const first = queue.enqueue({ name: 'slow', payload: {}, idempotencyKey: 'hold' })
  await sleep(15)
  let backpressureMessage = ''
  try {
    await queue.enqueue({ name: 'slow', payload: {}, idempotencyKey: 'overflow' })
  } catch (error: unknown) {
    backpressureMessage = error instanceof Error ? error.message : ''
  }
  release?.()
  await first
  return { backpressureMessage }
}

async function expiry() {
  const queue = new MemoryAsyncQueue({ retentionMs: 40, timeoutMs: 5000 })
  queue.register('job', async () => ({ ok: true }))
  const first = await queue.enqueue({ name: 'job', payload: {}, idempotencyKey: 'old' })
  await sleep(60)
  const second = await queue.enqueue({ name: 'job', payload: {}, idempotencyKey: 'old' })
  const old = await queue.get(first.id)
  return { expired: old === null, reborn: second.id !== first.id && second.status === 'completed' }
}

function bullmq() {
  let bullmqMessage = ''
  let redisMessage = ''
  try {
    assertMemoryDrivers('bullmq', 'memory')
  } catch (error: unknown) {
    bullmqMessage = error instanceof Error ? error.message : ''
  }
  try {
    assertMemoryDrivers('memory', 'redis')
  } catch (error: unknown) {
    redisMessage = error instanceof Error ? error.message : ''
  }
  return { bullmqMessage, redisMessage }
}

main().catch((error: unknown) => {
  console.error(error instanceof Error ? (error.stack ?? error.message) : error)
  process.exit(1)
})
