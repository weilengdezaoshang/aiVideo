import './dom.js'
import test, { afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ConnectedSessionPanel } from '../apps/web/canvas/session/connected-panel.js'
import type {
  PendingMessage,
  SessionApi,
  SessionSource,
  SessionSnapshot,
} from '../apps/web/canvas/session/store.js'

afterEach(() => {
  cleanup()
  localStorage.clear()
  window.sessionStorage.clear()
})

test('canvas conversation accepts consecutive messages during an active Run and saves first', async () => {
  const sent: PendingMessage[] = []
  const operations: string[] = []
  const snapshot: SessionSnapshot = {
    id: 'session',
    cursor: 0,
    summary: '之前已确认角色',
    messages: [],
    runs: [{ id: 'run', status: 'running', version: 1 }],
  }
  const api: SessionApi = {
    open: async () => snapshot,
    snapshot: async () => snapshot,
    send: async (_id, body) => {
      operations.push('send')
      sent.push(body)
    },
    events: () => '/events',
  }
  let closed = false
  const source: SessionSource = {
    addEventListener() {},
    close() {
      closed = true
    },
  }
  const sourceFactory = () => source
  const view = render(
    <ConnectedSessionPanel
      documentId="doc"
      api={api}
      source={sourceFactory}
      ensureSaved={async () => {
        operations.push('save')
      }}
      syncDocument={async () => {}}
      selectedAssets={() => ['image']}
    />,
  )
  await screen.findByText('执行中')
  const input = screen.getByRole('textbox', { name: '告诉助手你想做什么' })
  const budget = screen.getByRole('spinbutton', { name: '本轮积分预算' })
  fireEvent.change(budget, { target: { value: '20' } })
  for (const text of ['换成狗', '不对，换成兔子', '再暗一点']) {
    fireEvent.change(input, { target: { value: text } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
  }
  await waitFor(() => assert.equal(sent.length, 3))
  assert.deepEqual(
    sent.map((row) => row.message),
    ['换成狗', '不对，换成兔子', '再暗一点'],
  )
  assert.ok(sent.every((row) => row.credits_budget === 20 && row.selected_assets[0] === 'image'))
  assert.equal(new Set(sent.map((row) => row.client_message_id)).size, 3)
  assert.ok(operations.indexOf('save') < operations.indexOf('send'))
  view.unmount()
  assert.equal(closed, true)
})
