import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { randomUUID } from 'node:crypto'
import { createApplication } from '../src/app/create-app.js'
import { MemoryTraceWriter } from '../src/adapters/memory/index.js'
import type { LoggedEvent } from '../src/domain/model.js'
import { agentTraceV1Schema } from '../src/trace/schema.js'

const request = (prompt: string) => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json', Origin: 'http://localhost:5173' },
  body: JSON.stringify({ prompt }),
})

function events(text: string): LoggedEvent[] {
  return text.split('\n\n').flatMap((frame) => {
    const data = frame.split('\n').find((line) => line.startsWith('data:'))
    return data ? [JSON.parse(data.slice(5)) as LoggedEvent] : []
  })
}

function setup() {
  const traces = new MemoryTraceWriter()
  return { traces, ...createApplication({ ports: { traces }, llmDelayMs: 0, toolDelayMs: 0 }) }
}

test('health、校验、CORS、限流在创建轮次之前', async () => {
  const { api } = setup()
  assert.equal((await api.request('/health')).status, 200)
  assert.equal((await api.request('/v1/conversation', request('  '))).status, 400)
  assert.equal((await api.request('/v1/conversation', { ...request('a'), body: '{' })).status, 400)
  const response = await api.request('/v1/conversation', request('咖啡'))
  assert.equal(response.headers.get('access-control-allow-origin'), 'http://localhost:5173')
  await response.text()
  const denied = createApplication({
    ports: {
      limiter: {
        async allow() {
          return { ok: false, retryAfterMs: 1000 }
        },
      },
    },
  })
  assert.equal((await denied.api.request('/v1/conversation', request('咖啡'))).status, 429)
  assert.equal(
    (
      await api.request('/v1/stop_stream', {
        method: 'POST',
        body: JSON.stringify({ conversation_id: randomUUID(), turn_uuid: randomUUID() }),
        headers: { 'Content-Type': 'application/json' },
      })
    ).status,
    404,
  )
})

test('Host Happy Path：start → delta → loading → artifact → complete，Trace v1', async () => {
  const { api, traces, ports } = setup()
  const response = await api.request('/v1/conversation', request('一张咖啡海报'))
  const stream = events(await response.text())
  assert.equal(stream[0].kind, 'system')
  assert.equal(stream[0].kind === 'system' && stream[0].type, 'stream_start')
  assert.equal(
    stream.at(-1)?.kind === 'system' &&
      (stream.at(-1) as Extract<LoggedEvent, { kind: 'system' }>).type,
    'stream_complete',
  )
  assert.equal(
    stream.some((e) => e.kind === 'text_delta'),
    true,
  )
  const snapshots = stream.filter((e) => e.kind === 'message')
  assert.equal(
    snapshots.some((e) =>
      e.message.parts.some((p) => p.kind === 'tool_status' && p.state === 'loading'),
    ),
    true,
  )
  const final = snapshots.at(-1)!.message
  assert.equal(final.status, 'completed')
  assert.equal(final.parts.filter((p) => p.kind === 'artifact').length, 1)
  assert.equal(final.parts.filter((p) => p.kind === 'text').length, 1)
  const trace = agentTraceV1Schema.parse(traces.get(stream[0].runId))
  assert.equal(trace.version, '1')
  assert.equal(trace.conversationId, stream[0].conversation_id)
  assert.equal(trace.turns[0]!.metrics.credits, 40)
  assert.equal(trace.turns[0]!.outcome, 'success')
  assert.equal(
    trace.turns[0]!.toolCalls.some((t) => t.name === 'creative_agent_mcp_gen_text2image_v3'),
    true,
  )
  assert.ok(trace.turns[0]!.toolCalls[0]!.jobId)
  const logged = await ports.events.listByTurn(stream[0].turn_uuid)
  assert.equal(logged.length, stream.length)
  assert.deepEqual(
    logged.map((e) => e.seq),
    stream.map((e) => e.sequence),
  )
  assert.equal(new Set(stream.map((e) => e.sequence)).size, stream.length)
  assert.equal((await api.request('/mock/image.svg')).headers.get('content-type'), 'image/svg+xml')
  // conversation 级 seq：跨 turn 递增
  const last = await ports.events.getLastSeq(stream[0].conversation_id)
  assert.equal(last, stream.at(-1)!.sequence)
})

test('复杂需求先 handoff + todo_plan，再积分中断，无成功作品', async () => {
  const { api, traces } = setup()
  const stream = events(
    await (await api.request('/v1/conversation', request('三个广告镜头'))).text(),
  )
  assert.equal(
    stream.some((e) => e.kind === 'system' && e.type === 'interrupt_approval'),
    true,
  )
  assert.equal(
    stream.some((e) => e.kind === 'system' && e.type === 'todo_plan'),
    true,
  )
  assert.equal(
    stream.some((e) => e.kind === 'message' && e.message.parts.some((p) => p.kind === 'artifact')),
    false,
  )
  assert.equal(traces.get(stream[0].runId)?.turns[0]?.metrics.credits, 0)
})

test('停止运行中轮次且保留取消状态，Trace 可见', async () => {
  const traces = new MemoryTraceWriter()
  const { api, ports } = createApplication({
    ports: { traces },
    llmDelayMs: 0,
    streamCharDelayMs: 80,
    toolDelayMs: 200,
  })
  const response = await api.request('/v1/conversation', request('咖啡'))
  const reader = response.body!.getReader()
  const first = await reader.read()
  const initial = new TextDecoder().decode(first.value)
  const start = events(initial)[0]
  assert.ok(start)
  const stopped = await api.request('/v1/stop_stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ conversation_id: start.conversation_id, turn_uuid: start.turn_uuid }),
  })
  assert.equal(stopped.status, 200)
  let rest = ''
  while (true) {
    const chunk = await reader.read()
    if (chunk.done) {
      break
    }
    rest += new TextDecoder().decode(chunk.value)
  }
  const final = events(initial + rest)
  assert.equal(
    final.some((e) => e.kind === 'message' && e.message.status === 'cancelled'),
    true,
  )
  assert.equal(
    (await ports.sessions.getRuntimeTurn(start.conversation_id, start.turn_uuid))?.status,
    'cancelled',
  )
  assert.equal(traces.get(start.runId)?.turns[0]?.outcome, 'cancelled')
  const again = await api.request('/v1/stop_stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ conversation_id: start.conversation_id, turn_uuid: start.turn_uuid }),
  })
  assert.equal(again.status, 200)
})

test('工具失败产生 failed 快照与 stream_error，仍写 Trace', async () => {
  const traces = new MemoryTraceWriter()
  const { api } = createApplication({
    ports: { traces },
    llmDelayMs: 0,
    tools: [
      {
        name: 'creative_agent_mcp_gen_text2image_v3',
        async execute() {
          throw new Error('工具错误')
        },
      },
    ],
  })
  const stream = events(await (await api.request('/v1/conversation', request('咖啡'))).text())
  assert.equal(
    stream.some((e) => e.kind === 'system' && e.type === 'stream_error'),
    true,
  )
  assert.equal(
    stream.some((e) => e.kind === 'message' && e.message.parts.some((p) => p.kind === 'artifact')),
    false,
  )
  assert.equal(traces.get(stream[0].runId)?.turns[0]?.outcome, 'failed')
})

test('文件 Trace 写入独立目录，schema 校验；写入失败不可宣告完成', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'creation-trace-'))
  try {
    const { api } = createApplication({ traceDirectory: directory, llmDelayMs: 0, toolDelayMs: 0 })
    const stream = events(await (await api.request('/v1/conversation', request('咖啡'))).text())
    const trace = agentTraceV1Schema.parse(
      JSON.parse(await readFile(join(directory, `${stream[0].runId}.json`), 'utf8')),
    )
    assert.equal(trace.version, '1')
    assert.equal(trace.runId, stream[0].runId)
  } finally {
    await rm(directory, { recursive: true, force: true })
  }
  const { api } = createApplication({
    ports: {
      traces: {
        async write() {
          throw new Error('磁盘不可写')
        },
      },
    },
    llmDelayMs: 0,
    toolDelayMs: 0,
  })
  const stream = events(await (await api.request('/v1/conversation', request('咖啡'))).text())
  assert.equal(
    stream.some((e) => e.kind === 'system' && e.type === 'stream_complete'),
    false,
  )
  assert.equal(
    stream.some((e) => e.kind === 'system' && e.type === 'stream_error'),
    true,
  )
})
