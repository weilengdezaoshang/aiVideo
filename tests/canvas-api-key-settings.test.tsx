import './dom.js'
import test, { afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ApiKeySettings } from '../apps/web/canvas/api-key-settings.js'

const originalFetch = globalThis.fetch
const dialogPrototype = window.document.createElement('dialog').constructor.prototype
Object.defineProperty(dialogPrototype, 'showModal', {
  configurable: true,
  value(this: HTMLDialogElement) {
    this.setAttribute('open', '')
  },
})
afterEach(() => {
  cleanup()
  globalThis.fetch = originalFetch
})
const status = {
  configured: true,
  keyConfigured: true,
  model: 'saved-model',
  baseUrl: 'https://example.com/v1',
  timeoutMs: 45000,
  hostEnabled: true,
}

test('loads saved values, omits blank Key and clears a replacement Key after save', async () => {
  const sent: Record<string, unknown>[] = []
  globalThis.fetch = async (_input, init) => {
    if (init?.method === 'POST') {
      sent.push(JSON.parse(String(init.body)) as Record<string, unknown>)
    }
    return Response.json(status)
  }
  render(<ApiKeySettings onClose={() => {}} />)
  await waitFor(() =>
    assert.equal(screen.getByRole('button', { name: '保存配置' }).hasAttribute('disabled'), false),
  )
  assert.ok(screen.getByDisplayValue('saved-model'))
  fireEvent.click(screen.getByRole('button', { name: '保存配置' }))
  await screen.findByText('已保存。当前创作助手仍使用模板规划。')
  assert.equal('apiKey' in sent[0], false)
  fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'replacement-test-key' } })
  fireEvent.click(screen.getByRole('button', { name: '保存配置' }))
  await waitFor(() =>
    assert.equal((screen.getByLabelText('API Key') as HTMLInputElement).value, ''),
  )
  assert.equal(sent[1].apiKey, 'replacement-test-key')
})

test('unavailable configuration service keeps save disabled and explains failure', async () => {
  globalThis.fetch = async () => Response.json({}, { status: 403 })
  render(<ApiKeySettings onClose={() => {}} />)
  await screen.findByRole('status')
  assert.equal(screen.getByRole('button', { name: '保存配置' }).hasAttribute('disabled'), true)
})
