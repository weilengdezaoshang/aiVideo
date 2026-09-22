import test from 'node:test'
import assert from 'node:assert/strict'
import { RecognitionOperation } from '../apps/web/canvas/flows/recognition-operation.js'

test('取消后即使响应迟到也不解析或应用，并只发送一次取消', async () => {
  const original = globalThis.fetch
  let resolve!: (response: Response) => void
  const calls: string[] = []
  globalThis.fetch = async (url, init) => {
    calls.push(`${init?.method} ${url}`)
    if (init?.method === 'DELETE') {
      return new Response('{}')
    }
    return new Promise<Response>((r) => {
      resolve = r
    })
  }
  try {
    const operation = new RecognitionOperation(() => true)
    const pending = operation.detect('source')
    operation.cancel()
    operation.cancel()
    resolve(new Response('not even valid JSON'))
    await assert.rejects(pending, { name: 'AbortError' })
    assert.equal(calls.filter((call) => call.startsWith('DELETE')).length, 1)
    assert.equal(operation.signal.aborted, true)
  } finally {
    globalThis.fetch = original
  }
})

test('解析响应期间源图替换使旧操作失效，新操作不受影响', async () => {
  const original = globalThis.fetch
  let matches = true
  let resolve!: (value: unknown) => void
  const old = new RecognitionOperation(() => matches)
  globalThis.fetch = async () =>
    ({
      ok: true,
      json: () =>
        new Promise((r) => {
          resolve = r
        }),
    }) as Response
  try {
    const pending = old.detect('source')
    await Promise.resolve()
    matches = false
    const next = new RecognitionOperation(() => true)
    resolve({ operationId: old.id, urls: { original: '/old.png' } })
    await assert.rejects(pending, { name: 'AbortError' })
    assert.equal(next.current(), true)
    assert.notEqual(next.id, old.id)
  } finally {
    globalThis.fetch = original
  }
})

test('只接受匹配的操作结果，完成后取消不会影响已提交任务', async () => {
  const original = globalThis.fetch
  const operation = new RecognitionOperation(() => true)
  let calls = 0
  globalThis.fetch = async () => {
    calls++
    return Response.json({ operationId: operation.id, urls: { original: '/mask.png' } })
  }
  try {
    assert.equal(await operation.detect('source'), '/mask.png')
    operation.complete()
    operation.cancel()
    assert.equal(calls, 1)
    assert.equal(operation.current(), false)
    const invalid = new RecognitionOperation(() => true)
    await assert.rejects(invalid.detect('source'), /不完整/)
  } finally {
    globalThis.fetch = original
  }
})
