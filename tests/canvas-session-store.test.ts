import './dom.js'
import test, { afterEach } from 'node:test'
import assert from 'node:assert/strict'
import {
  createSessionStore,
  type SessionApi,
  type SessionSnapshot,
  type SessionSource,
} from '../apps/web/canvas/session/store.js'

afterEach(() => localStorage.clear())
const tick = () => new Promise<void>((resolve) => setImmediate(resolve))
const snapshot = (cursor = 0): SessionSnapshot => ({
  id: 'session',
  cursor,
  summary: '',
  messages: [],
  runs: [],
})
function setup(overrides: Partial<SessionApi> = {}) {
  const events = new Map<string, (event: { data?: string; lastEventId?: string }) => void>()
  const source: SessionSource = {
    addEventListener: (name, listener) => {
      events.set(name, listener)
    },
    close() {},
  }
  const api: SessionApi = {
    open: async () => snapshot(),
    snapshot: async () => snapshot(),
    send: async () => ({}),
    events: (id, cursor) => `${id}?after=${cursor}`,
    ...overrides,
  }
  let nextId = 0
  const store = createSessionStore({
    documentId: 'doc',
    clientId: 'client',
    api,
    storage: localStorage,
    source: () => source,
    newId: () => String(++nextId),
  })
  return { store, events }
}

test('session retries keep the original idempotency key and preserve message order', async () => {
  const received: string[] = []
  let unavailable = true
  const { store } = setup({
    send: async (_id, body) => {
      received.push(body.client_message_id)
      if (unavailable) {
        throw new Error('offline')
      }
    },
  })
  const stop = store.start()
  await tick()
  await store.send('换成狗')
  await store.send('不对，换成兔子')
  assert.equal(store.getSnapshot().pending.length, 2)
  unavailable = false
  await store.retry()
  assert.deepEqual(received, ['1', '1', '1', '2'])
  assert.equal(store.getSnapshot().pending.length, 0)
  stop()
})

test('duplicate session events cannot roll the database cursor backwards', async () => {
  let cursor = 0
  const { store, events } = setup({ snapshot: async () => snapshot(cursor) })
  const stop = store.start()
  await tick()
  cursor = 5
  events.get('session')?.({ lastEventId: '5' })
  await tick()
  events.get('session')?.({ lastEventId: '3' })
  assert.equal(store.getSnapshot().snapshot?.cursor, 5)
  stop()
})

test('an event arriving during a snapshot fetch forces another database read', async () => {
  let reads = 0
  let release: ((value: SessionSnapshot) => void) | undefined
  const { store, events } = setup({
    snapshot: async () => {
      reads++
      if (reads === 2) {
        return new Promise<SessionSnapshot>((resolve) => {
          release = resolve
        })
      }
      return snapshot(reads === 1 ? 0 : 2)
    },
  })
  const stop = store.start()
  await tick()
  events.get('session')?.({ lastEventId: '1' })
  await tick()
  events.get('session')?.({ lastEventId: '2' })
  release?.(snapshot(1))
  await tick()
  assert.equal(reads, 3)
  assert.equal(store.getSnapshot().snapshot?.cursor, 2)
  stop()
})

test('late open response after unmount cannot reconnect or overwrite a restarted store', async () => {
  let resolve: ((value: SessionSnapshot) => void) | undefined
  const { store } = setup({
    open: () =>
      new Promise<SessionSnapshot>((done) => {
        resolve = done
      }),
  })
  const stop = store.start()
  stop()
  resolve?.(snapshot(9))
  await tick()
  assert.equal(store.getSnapshot().snapshot, null)
  assert.equal(store.getSnapshot().connected, false)
})

test('an authoritative SSE reset repairs a future cursor while ordinary reads cannot regress it', async () => {
  const { store, events } = setup({
    open: async () => snapshot(100),
    snapshot: async () => snapshot(100),
  })
  const stop = store.start()
  await tick()
  events.get('snapshot')?.({ data: JSON.stringify(snapshot(2)), lastEventId: '2' })
  assert.equal(store.getSnapshot().snapshot?.cursor, 2)
  stop()
})

test('two windows cannot overwrite offline messages and a fresh window replays original identities', async () => {
  let offline = true
  const delivered: string[] = []
  const api: SessionApi = {
    open: async () => snapshot(),
    snapshot: async () => snapshot(),
    events: () => '/events',
    send: async (_id, body) => {
      if (offline) {
        throw new Error('offline')
      }
      delivered.push(`${body.client_id}:${body.client_message_id}`)
    },
  }
  const make = (clientId: string) =>
    createSessionStore({
      documentId: 'doc',
      clientId,
      api,
      storage: localStorage,
      newId: () => '1',
      source: () => ({ addEventListener() {}, close() {} }),
    })
  const left = make('left')
  const right = make('right')
  const stopLeft = left.start()
  const stopRight = right.start()
  await tick()
  await left.send('换成狗')
  await right.send('不对，换成兔子')
  stopLeft()
  stopRight()
  offline = false
  const returned = make('new-window')
  const stop = returned.start()
  await tick()
  assert.deepEqual(delivered, ['left:1', 'right:1'])
  assert.equal(returned.getSnapshot().pending.length, 0)
  stop()
})

test('a different authenticated Session never replays another user’s local pending messages', async () => {
  const first = setup({
    send: async () => {
      throw new Error('offline')
    },
  }).store
  const stopFirst = first.start()
  await tick()
  await first.send('我的消息')
  stopFirst()
  const sent: string[] = []
  const second = setup({
    open: async () => ({ ...snapshot(), id: 'different-user-session' }),
    snapshot: async () => ({ ...snapshot(), id: 'different-user-session' }),
    send: async (_id, body) => {
      sent.push(body.message)
    },
  }).store
  const stopSecond = second.start()
  await tick()
  assert.deepEqual(sent, [])
  assert.equal(second.getSnapshot().pending.length, 0)
  stopSecond()
})
