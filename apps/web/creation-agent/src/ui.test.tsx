import '../../../../tests/dom.js'
import React from 'react'
import { afterEach, test } from 'node:test'
import assert from 'node:assert/strict'
import { render, fireEvent, cleanup, act } from '@testing-library/react'
import { Composer } from './Composer.js'
import { ConfirmPart, PartView } from './parts/PartView.js'
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

test('计划 title 默认可见，thinking/工具默认折叠且可展开', async () => {
  const { PartView } = await import('./parts/PartView.js')
  const open = () => {}
  const plan = render(
    <PartView
      part={{
        kind: 'plan',
        steps: [{ id: 'a', title: '生成关键帧并合成视频', status: 'pending' }],
      }}
      number={0}
      open={open}
    />,
  )
  assert.ok(plan.getByText('生成关键帧并合成视频'))
  for (const part of [
    { kind: 'thinking' as const, text: '详细推理', summary: '思考中' },
    { kind: 'tool_call' as const, summary: '调用视频工具', detail: '调用参数' },
    { kind: 'tool_result' as const, summary: '视频工具完成', detail: '返回结果' },
    {
      kind: 'tool_status' as const,
      callId: 'v',
      toolName: 'video_generate',
      label: '生成视频',
      state: 'loading' as const,
    },
  ]) {
    const view = render(<PartView part={part} number={0} open={open} />)
    const details = view.container.querySelector('details')!
    assert.equal(details.open, false)
    assert.ok(details.querySelector('summary')?.textContent)
    details.open = true
    fireEvent(details, new Event('toggle'))
    assert.equal(details.open, true)
    view.unmount()
  }
})

test('并行进程显示真实完成数，跳过步骤与样片角标可见', () => {
  const view = render(
    <PartView
      part={{
        kind: 'plan',
        steps: [
          { id: 'a', title: '正面', status: 'completed' },
          { id: 'b', title: '背面', status: 'in_progress' },
          { id: 'c', title: '左侧', status: 'in_progress' },
          { id: 'd', title: '合成', status: 'skipped' },
        ],
      }}
      number={0}
      open={() => {}}
    />,
  )
  assert.ok(view.getByText('进程 1/4'))
  assert.ok(view.getByText('正在并行执行 2 个步骤'))
  assert.ok(view.getByText('· 依赖失败，未执行'))
  assert.equal((view.getByRole('progressbar') as HTMLProgressElement).value, 1)
  view.rerender(
    <PartView
      part={{
        kind: 'artifact',
        id: 'preview',
        type: 'image',
        url: '/fixture.png',
        status: 'success',
        tier: 'preview',
      }}
      number={1}
      open={() => {}}
    />,
  )
  assert.ok(view.getByText('样片预览'))
})
