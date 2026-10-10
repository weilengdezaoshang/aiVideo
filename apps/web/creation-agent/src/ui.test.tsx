import '../../../../tests/dom.js'
import React from 'react'
import { afterEach, test } from 'node:test'
import assert from 'node:assert/strict'
import { render, fireEvent, cleanup, act } from '@testing-library/react'
import { Composer } from './Composer.js'
import { ConfirmPart } from './parts/PartView.js'
import { MessageList } from './MessageList.js'
import { App } from './App.js'
import { agentStore } from './store.js'
afterEach(() => {
  cleanup()
  agentStore.getState().stop()
  agentStore.setState({
    messageIds: [],
    messagesById: {},
    status: 'idle',
    draft: '',
    toast: '',
    faults: { connection: false, tool: false, resume: false },
  })
})
test('输入、换行、输入法、发送和忙碌禁用', () => {
  const view = render(<Composer />)
  const input = view.getByRole('textbox') as HTMLTextAreaElement
  assert.equal((view.getByRole('button', { name: '发送 ↑' }) as HTMLButtonElement).disabled, true)
  fireEvent.change(input, { target: { value: '咖啡' } })
  fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })
  assert.equal(agentStore.getState().status, 'idle')
  fireEvent.compositionStart(input)
  fireEvent.keyDown(input, { key: 'Enter' })
  assert.equal(agentStore.getState().status, 'idle')
  fireEvent.compositionEnd(input)
  fireEvent.keyDown(input, { key: 'Enter' })
  assert.equal(agentStore.getState().status, 'sending')
  assert.equal(input.disabled, true)
  for (const status of ['streaming', 'awaiting_user'] as const) {
    act(() => agentStore.setState({ status }))
    assert.equal(input.disabled, true)
  }
  fireEvent.click(view.getByRole('button', { name: /停止/ }))
  assert.equal(input.disabled, false)
  assert.equal(document.activeElement, input)
})
test('确认出现聚焦按钮，忙碌时按钮禁用', () => {
  const view = render(
    <ConfirmPart
      part={{
        kind: 'confirm',
        confirmId: 'one',
        title: '确认镜头',
        estimateCredits: 120,
        summary: '生成三张图',
        state: 'pending',
      }}
    />,
  )
  assert.equal(document.activeElement, view.getByRole('button', { name: '确认' }))
  act(() => agentStore.setState({ confirming: true }))
  assert.equal(
    (view.getByRole('button', { name: '正在确认…' }) as HTMLButtonElement).disabled,
    true,
  )
  assert.equal((view.getByRole('button', { name: '取消' }) as HTMLButtonElement).disabled, true)
})
test('上翻显示新内容，点击回到底部', () => {
  const view = render(<MessageList open={() => {}} />)
  const scroll = view.container.querySelector('.message-scroll') as HTMLElement
  Object.defineProperties(scroll, {
    scrollHeight: { configurable: true, value: 1000 },
    clientHeight: { configurable: true, value: 200 },
  })
  scroll.scrollTop = 0
  fireEvent.scroll(scroll)
  act(() =>
    agentStore.setState({
      messageIds: ['m'],
      messagesById: {
        m: {
          id: 'm',
          role: 'assistant',
          status: 'streaming',
          parts: [{ kind: 'text', text: '新内容' }],
        },
      },
    }),
  )
  fireEvent.click(view.getByRole('button', { name: '↓ 有新内容' }))
  assert.equal(scroll.scrollTop, 1000)
  assert.equal(view.queryByText('↓ 有新内容'), null)
})
test('作品放大，关闭后焦点返回触发按钮', () => {
  window.HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
    this.setAttribute('open', '')
  }
  window.HTMLDialogElement.prototype.close = function (this: HTMLDialogElement) {
    this.removeAttribute('open')
    this.dispatchEvent(new Event('close'))
  }
  agentStore.setState({
    messageIds: ['m'],
    messagesById: {
      m: {
        id: 'm',
        role: 'assistant',
        status: 'completed',
        parts: [
          { kind: 'artifact', id: 'a', type: 'image', url: '/coffee-1.svg', status: 'success' },
        ],
      },
    },
  })
  const view = render(<App />)
  const button = view.getByRole('button', { name: '放大作品 1' })
  fireEvent.click(button)
  assert.equal(view.getByRole('dialog').hasAttribute('open'), true)
  fireEvent.click(view.getByRole('button', { name: '关闭作品预览' }))
  assert.equal(document.activeElement, button)
})

test('已取消计划不显示进行中 spinner，并明确展示取消状态', async () => {
  const { PartView } = await import('./parts/PartView.js')
  const view = render(
    <PartView
      number={0}
      open={() => {}}
      part={{
        kind: 'plan',
        steps: [
          { id: 'done', text: '策划', status: 'completed' },
          { id: 'cancelled', text: '生成画面', status: 'cancelled' },
          { id: 'pending', text: '整理', status: 'pending' },
        ],
      }}
    />,
  )
  assert.equal(view.queryByLabelText('进行中'), null)
  assert.equal(view.container.querySelectorAll('.spinner').length, 0)
  assert.match(view.getByRole('heading').textContent ?? '', /已取消/)
})
