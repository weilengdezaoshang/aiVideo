import test from 'node:test'
import assert from 'node:assert/strict'
import { withSessionCsrf } from '../apps/web/shared/session-fetch.js'

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
