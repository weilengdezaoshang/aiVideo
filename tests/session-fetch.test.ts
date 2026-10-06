import test from 'node:test'
import assert from 'node:assert/strict'
import { withSessionCsrf } from '../apps/web/shared/session-fetch.js'
import { createAutoSaver } from '../apps/web/canvas/state/autosave.js'

test('CSRF is attached only to same-origin mutations, preserving headers', async () => {
  const calls: RequestInit[] = []
  const fake: typeof fetch = async (_input, init) => {
    calls.push(init ?? {})
    return new Response('{}')
  }
  const wrapped = withSessionCsrf(fake, 'https://app.test', () => '__Host-aivideo-csrf=token')
  await wrapped('/api/generate', { method: 'POST', headers: { 'Idempotency-Key': 'request' } })
  assert.equal(new Headers(calls[0].headers).get('X-CSRF-Token'), 'token')
  assert.equal(new Headers(calls[0].headers).get('Idempotency-Key'), 'request')
  await wrapped('https://external.test', { method: 'POST' })
  await wrapped('/api/jobs')
  assert.equal(new Headers(calls[1].headers).has('X-CSRF-Token'), false)
  assert.equal(new Headers(calls[2].headers).has('X-CSRF-Token'), false)
})

test('unload save carries the same CSRF protection and revision as ordinary writes', async () => {
  const calls: RequestInit[] = []
  const fetchImpl = withSessionCsrf(
    async (_input, init) => {
      calls.push(init ?? {})
      return new Response('{}')
    },
    'https://app.test',
    () => '__Host-aivideo-csrf=unload-token',
  )
  const saver = createAutoSaver({
    docId: 'doc',
    getDoc: () => ({ id: 'doc', name: 'unsaved edit', objects: {}, order: [], revision: 7 }),
    fetchImpl,
  })
  saver.markDirty()
  saver.flushOnUnload()
  assert.equal(calls.length, 1)
  assert.equal(calls[0].keepalive, true)
  assert.equal(new Headers(calls[0].headers).get('X-CSRF-Token'), 'unload-token')
  assert.deepEqual(JSON.parse(String(calls[0].body)), {
    id: 'doc',
    name: 'unsaved edit',
    objects: {},
    order: [],
    revision: 7,
    baseRevision: 7,
    storyboard: null,
    timeline: null,
  })
})
