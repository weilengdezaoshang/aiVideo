import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  MemoryEventLog,
  MemorySessionStore,
  MemoryTaskQueue,
} from '../src/adapters/memory/index.js'
import { migrateLegacyTrace } from '../src/trace/migrate.js'
import { agentTraceV1Schema } from '../src/trace/schema.js'

test('SessionStore：conversation CRUD + interrupt snapshot', async () => {
  const sessions = new MemorySessionStore()
  const c = await sessions.createConversation({ title: 'demo' })
  assert.equal(c.activeAgent, 'host')
  assert.equal((await sessions.getConversation(c.id))?.title, 'demo')
  c.title = 'updated'
  c.updatedAt = new Date().toISOString()
  await sessions.saveConversation(c)
  assert.equal((await sessions.getConversation(c.id))?.title, 'updated')

  const turn = await sessions.beginTurn('hello', c.id)
  assert.equal(turn.conversationId, c.id)
  const record = await sessions.getTurn(turn.turnId)
  assert.equal(record?.status, 'running')

  await sessions.saveInterruptSnapshot(turn.turnId, {
    confirmId: 'cf_1',
    reason: 'credits',
    estimateCredits: 40,
    activeAgent: 'host',
    savedAt: new Date().toISOString(),
  })
  assert.equal((await sessions.getTurn(turn.turnId))?.status, 'interrupted')
  const snap = await sessions.getInterruptSnapshot(turn.turnId)
  assert.equal(snap?.confirmId, 'cf_1')
  await sessions.clearInterruptSnapshot(turn.turnId)
  assert.equal(await sessions.getInterruptSnapshot(turn.turnId), null)
  const turns = await sessions.listTurns(c.id)
  assert.equal(turns.length, 1)
})

test('EventLog：conversation 级 seq + readFrom / getLastSeq', async () => {
  const events = new MemoryEventLog()
  const conversationId = 'conv-a'
  const t1 = 'turn-1'
  const t2 = 'turn-2'
  const a = await events.append({
    conversationId,
    turnId: t1,
    event: 'system',
    data: { kind: 'system', type: 'stream_start' },
  })
  assert.equal(a.seq, 1)
  await events.append({
    conversationId,
    turnId: t1,
    event: 'delta',
    data: { kind: 'text_delta', text: 'a' },
  })
  await events.append({
    conversationId,
    turnId: t2,
    event: 'system',
    data: { kind: 'system', type: 'stream_start' },
  })
  assert.equal(await events.getLastSeq(conversationId), 3)
  const from1 = await events.readFrom(conversationId, 1)
  assert.deepEqual(
    from1.map((e) => e.seq),
    [2, 3],
  )
  const onlyT2 = await events.readFrom(conversationId, 0, { turnId: t2 })
  assert.equal(onlyT2.length, 1)
  assert.equal(onlyT2[0]!.turnId, t2)
  const limited = await events.readFrom(conversationId, 0, { limit: 2 })
  assert.equal(limited.length, 2)
  const byTurn = await events.listByTurn(t1)
  assert.equal(byTurn.length, 2)
})

test('TaskQueue：enqueue 幂等；同 idempotencyKey 不双跑', async () => {
  const queue = new MemoryTaskQueue()
  let runs = 0
  queue.register('creative_agent_mcp_gen_text2image_v3', async (payload) => {
    runs += 1
    return { ok: true, payload }
  })
  const first = await queue.enqueue({
    name: 'creative_agent_mcp_gen_text2image_v3',
    payload: { prompt: '咖啡' },
    idempotencyKey: 'tc_1',
  })
  const second = await queue.enqueue({
    name: 'creative_agent_mcp_gen_text2image_v3',
    payload: { prompt: '咖啡' },
    idempotencyKey: 'tc_1',
  })
  assert.equal(first.id, second.id)
  assert.equal(runs, 1)
  assert.equal(first.status, 'completed')
  assert.equal((await queue.getByIdempotencyKey('tc_1'))?.id, first.id)
  assert.equal((await queue.get(first.id))?.status, 'completed')
})

test('migrateLegacyTrace：旧 schemaVersion 转为 version:1', () => {
  const legacy = {
    schemaVersion: 1,
    runId: '11111111-1111-1111-1111-111111111111',
    conversation_id: '22222222-2222-2222-2222-222222222222',
    turn_uuid: '33333333-3333-3333-3333-333333333333',
    agent: 'host',
    prompt: '咖啡',
    startedAt: '2026-10-09T12:00:00.000Z',
    endedAt: '2026-10-09T12:00:01.000Z',
    status: 'completed',
    credits: 40,
    spans: [
      {
        name: 'llm.host',
        startedAt: '2026-10-09T12:00:00.000Z',
        endedAt: '2026-10-09T12:00:00.500Z',
        status: 'completed',
      },
      {
        name: 'creative_agent_mcp_gen_text2image_v3',
        startedAt: '2026-10-09T12:00:00.500Z',
        endedAt: '2026-10-09T12:00:01.000Z',
        status: 'completed',
      },
    ],
    events: [
      {
        kind: 'system',
        type: 'stream_start',
        sequence: 1,
        timestamp: '2026-10-09T12:00:00.000Z',
      },
    ],
    output: {},
  }
  const migrated = migrateLegacyTrace(legacy)
  const parsed = agentTraceV1Schema.parse(migrated)
  assert.equal(parsed.version, '1')
  assert.equal(parsed.conversationId, legacy.conversation_id)
  assert.equal(parsed.turns[0]!.outcome, 'success')
  assert.equal(parsed.turns[0]!.metrics.credits, 40)
  assert.equal(parsed.turns[0]!.toolCalls[0]!.name, 'creative_agent_mcp_gen_text2image_v3')
})
