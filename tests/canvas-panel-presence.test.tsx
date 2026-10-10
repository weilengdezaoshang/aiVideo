import './dom.js'
import assert from 'node:assert/strict'
import test, { afterEach } from 'node:test'
import { useState } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { ComposerPresence, SidebarPresence } from '../apps/web/canvas/panel-presence.js'

afterEach(cleanup)

function PanelHarness() {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button onClick={() => setOpen((value) => !value)}>切换面板</button>
      <ComposerPresence sidebarOpen={open}>
        <textarea aria-label="画布草稿" defaultValue="画布输入" />
      </ComposerPresence>
      <SidebarPresence open={open}>
        <textarea aria-label="对话草稿" defaultValue="对话输入" />
      </SidebarPresence>
    </>
  )
}

test('画布输入和侧栏互斥，退出面板仍保留DOM供动画完成', () => {
  const result = render(<PanelHarness />)
  const panel = result.container.querySelector('aside')
  const composer = result.container.querySelector('.composer-presence')
  assert.ok(panel)
  assert.ok(composer)
  assert.equal(panel.hasAttribute('hidden'), false)
  assert.equal(panel.getAttribute('aria-hidden'), 'true')
  assert.equal(panel.hasAttribute('inert'), true)
  assert.equal(composer.hasAttribute('inert'), false)
  assert.equal(screen.queryByRole('textbox', { name: '对话草稿' }), null)
  fireEvent.click(screen.getByRole('button', { name: '切换面板' }))
  assert.equal(composer.getAttribute('aria-hidden'), 'true')
  assert.equal(composer.hasAttribute('inert'), true)
  assert.equal(panel.hasAttribute('inert'), false)
  assert.equal(screen.queryByRole('textbox', { name: '画布草稿' }), null)
  fireEvent.click(screen.getByRole('button', { name: '切换面板' }))
  assert.equal(result.container.querySelector('aside'), panel)
  assert.equal(panel.hasAttribute('hidden'), false)
  assert.equal(panel.hasAttribute('inert'), true)
  assert.equal(composer.getAttribute('aria-hidden'), 'false')
})

test('快速反向切换不重挂输入框，两边未发送草稿各自保留', () => {
  const result = render(<PanelHarness />)
  const manual = screen.getByRole('textbox', { name: '画布草稿' }) as HTMLTextAreaElement
  fireEvent.change(manual, { target: { value: '未发送的画布提示词' } })
  fireEvent.click(screen.getByRole('button', { name: '切换面板' }))
  const chat = screen.getByRole('textbox', { name: '对话草稿' }) as HTMLTextAreaElement
  fireEvent.change(chat, { target: { value: '未发送的对话提示词' } })
  for (let index = 0; index < 5; index++) {
    fireEvent.click(screen.getByRole('button', { name: '切换面板' }))
  }
  assert.equal(screen.getByRole('textbox', { name: '画布草稿' }), manual)
  assert.equal(manual.value, '未发送的画布提示词')
  assert.equal(chat.value, '未发送的对话提示词')
  assert.equal(result.container.querySelectorAll('textarea').length, 2)
})
