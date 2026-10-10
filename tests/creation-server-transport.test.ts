import { test } from 'node:test'
import assert from 'node:assert/strict'
import { randomUUID } from 'node:crypto'
import { createServerTransport, readSse } from '../apps/web/creation-agent/src/server-transport.js'
import type { StreamEvent } from '../apps/web/creation-agent/src/model.js'
const identifiers = { conversation_id: randomUUID(), turn_uuid: randomUUID(), runId: randomUUID() }
function response(events: object[]) {
  const content = events
    .map(
      (e, i) =>
        `event: message\r\ndata: ${JSON.stringify({ ...identifiers, sequence: i + 1, ...e })}\r\n\r\n`,
    )
    .join('')
  const bytes = new TextEncoder().encode(content)
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (let i = 0; i < bytes.length; i += 7) {
          controller.enqueue(bytes.slice(i, i + 7))
        }
        controller.close()
      },
    }),
    { headers: { 'Content-Type': 'text/event-stream' } },
  )
}
test('SSE 分片、中文 UTF8、CRLF、多帧解析', async () => {
  const received: unknown[] = []
  for await (const event of readSse(
    response([
      { kind: 'text_delta', text: '咖啡画面' },
      { kind: 'system', type: 'stream_complete' },
    ]),
    new AbortController().signal,
  )) {
    received.push(event)
  }
  assert.equal(received.length, 2)
  assert.equal((received[0] as { text: string }).text, '咖啡画面')
})
test('服务端传输转前端事件，保留实际 prompt 和 conversation_id，快照不重复文字', async () => {
  const original = globalThis.fetch
  const requests: { url: string; body: Record<string, unknown> }[] = []
  globalThis.fetch = async (url, init) => {
    requests.push({
      url: String(url),
      body: JSON.parse(String(init?.body)) as Record<string, unknown>,
    })
    if (String(url).endsWith('stop_stream')) {
      return Response.json({ status: 'cancelled' })
    }
    return response([
      { kind: 'system', type: 'stream_start' },
      { kind: 'text_delta', text: '你好' },
      {
        kind: 'message',
        message: {
          id: 'assistant',
          role: 'assistant',
          status: 'completed',
          parts: [{ kind: 'text', text: '你好' }],
        },
      },
      { kind: 'system', type: 'stream_complete', status: 'completed' },
    ])
  }
  try {
    const transport = createServerTransport()
    const received: StreamEvent[] = []
    await transport.start(
      'local-run',
      false,
      new AbortController().signal,
      (e) => received.push(e),
      { connection: false, tool: false, resume: false },
      '实际创作需求',
    )
    assert.deepEqual(
      received.map((e) => e.type),
      ['stream_start', 'text_delta', 'message_snapshot', 'stream_complete'],
    )
    assert.equal(
      received.every((e) => e.runId === 'local-run'),
      true,
    )
    await transport.start(
      'local-2',
      false,
      new AbortController().signal,
      () => {},
      { connection: false, tool: false, resume: false },
      '下一轮',
    )
    assert.equal(requests[0].body.prompt, '实际创作需求')
    assert.equal(requests[1].body.conversation_id, identifiers.conversation_id)
  } finally {
    globalThis.fetch = original
  }
})
test('流提前断开、非 SSE、HTTP 错误不能默认为完成', async () => {
  const original = globalThis.fetch
  try {
    for (const result of [
      response([{ kind: 'system', type: 'stream_start' }]),
      new Response('{}'),
      new Response('error', { status: 500 }),
    ]) {
      globalThis.fetch = async () => result
      await assert.rejects(
        createServerTransport().start(
          'local',
          false,
          new AbortController().signal,
          () => {},
          { connection: false, tool: false, resume: false },
          '咖啡',
        ),
      )
    }
  } finally {
    globalThis.fetch = original
  }
})
