import './dom.js'
import test, { afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { userEvent } from '@testing-library/user-event'
import { AgentActivityLog, AgentThinking } from '../apps/web/canvas/agent/activity-log.js'
import { jumpTo, toolAnchor } from '../apps/web/canvas/agent-answer.js'
import type { AgentRun, AgentToolCall } from '../apps/web/canvas/agent/types.js'
afterEach(cleanup)
const tool = (id: string, status: AgentToolCall['status'] = 'completed'): AgentToolCall => ({
  id,
  name: 'read_canvas',
  kind: 'read',
  status,
  arguments: { title: id },
  evidenceId: id,
})
const run = (calls: AgentToolCall[]): AgentRun => ({
  id: 'activity-run',
  clientRequestId: 'request',
  message: '',
  status: 'running',
  reply: null,
  error: null,
  modelId: '',
  baseRevision: 0,
  modelCalls: [],
  toolCalls: calls,
  createdAt: '',
  updatedAt: '',
})
const row = (id: string) => screen.getByRole('button', { name: new RegExp(`读取\\s*${id}`) })
test('运行与完成默认收起；手动展开在快照替换与重新挂载后保留', async () => {
  const a = tool('auto-timer', 'running')
  const view = render(<AgentActivityLog run={run([a])} current />)
  assert.equal(row(a.id).getAttribute('aria-expanded'), 'false')
  view.rerender(<AgentActivityLog run={run([{ ...a, status: 'completed' }])} current />)
  assert.equal(row(a.id).getAttribute('aria-expanded'), 'false')
  await waitFor(() => assert.equal(row(a.id).getAttribute('aria-expanded'), 'false'))
  fireEvent.click(row(a.id))
  view.rerender(
    <AgentActivityLog run={structuredClone(run([{ ...a, status: 'completed' }]))} current />,
  )
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 350))
  })
  assert.equal(row(a.id).getAttribute('aria-expanded'), 'true')
  view.unmount()
  render(<AgentActivityLog run={run([{ ...a, status: 'completed' }])} current={false} />)
  assert.equal(row(a.id).getAttribute('aria-expanded'), 'true')
})
test('用户手动收起运行工具后，后续快照不再自动展开', () => {
  const a = tool('manual-close', 'running')
  const view = render(<AgentActivityLog run={run([a])} current />)
  fireEvent.click(row(a.id))
  fireEvent.click(row(a.id))
  view.rerender(<AgentActivityLog run={structuredClone(run([a]))} current />)
  assert.equal(row(a.id).getAttribute('aria-expanded'), 'false')
})
test('新一轮开始仍保持未操作的运行行收起', () => {
  const a = tool('previous-run', 'running')
  const view = render(<AgentActivityLog run={run([a])} current />)
  assert.equal(row(a.id).getAttribute('aria-expanded'), 'false')
  view.rerender(<AgentActivityLog run={run([a])} current={false} />)
  assert.equal(row(a.id).getAttribute('aria-expanded'), 'false')
})
test('连续三次读取分组，引用可展开隐藏分组和子行；子行不显示类别图标', () => {
  const calls = [tool('group-a'), tool('group-b'), tool('group-c')]
  const view = render(<AgentActivityLog run={run(calls)} current />)
  const group = screen.getByRole('button', { name: /读取\s*· 3 次调用/ })
  assert.equal(group.getAttribute('aria-expanded'), 'false')
  assert.ok(document.getElementById(toolAnchor('activity-run', 'group-b')))
  act(() => jumpTo(toolAnchor('activity-run', 'group-b')))
  assert.equal(group.getAttribute('aria-expanded'), 'true')
  assert.equal(row('group-b').getAttribute('aria-expanded'), 'true')
  assert.equal(view.container.querySelectorAll('.is-nested .next-agent-activity-icon').length, 0)
})
test('分组运行时保持收起；用户展开子行后分组保持展开', async () => {
  const calls = [tool('group-live-a'), tool('group-live-b', 'running'), tool('group-live-c')]
  const view = render(<AgentActivityLog run={run(calls)} current />)
  const group = screen.getByRole('button', { name: /读取\s*· 3 次调用/ })
  assert.equal(group.getAttribute('aria-expanded'), 'false')
  view.rerender(
    <AgentActivityLog run={run(calls.map((call) => ({ ...call, status: 'completed' })))} current />,
  )
  await waitFor(() => assert.equal(group.getAttribute('aria-expanded'), 'false'))
  fireEvent.click(group)
  fireEvent.click(row('group-live-a'))
  view.rerender(<AgentActivityLog run={run(calls)} current={false} />)
  assert.equal(group.getAttribute('aria-expanded'), 'true')
  assert.equal(row('group-live-a').getAttribute('aria-expanded'), 'true')
})
test('键盘Enter与空格展开；参数默认收起；状态和失败提示正确', async () => {
  const user = userEvent.setup()
  render(
    <AgentActivityLog
      run={run(
        [
          tool('keyboard', 'failed'),
          tool('approval', 'waiting_approval'),
          tool('late', 'late'),
        ].map((call) => ({ ...call, error: call.status === 'failed' ? '读取失败原因' : null })),
      )}
      current
    />,
  )
  // Three reads are grouped, so expand the group first.
  await user.click(screen.getByRole('button', { name: /3 次调用/ }))
  const button = row('keyboard')
  button.focus()
  await user.keyboard('{Enter}')
  assert.equal(button.getAttribute('aria-expanded'), 'true')
  const parameters = screen.getByRole('button', { name: '展开参数' })
  assert.equal(parameters.getAttribute('aria-expanded'), 'false')
  fireEvent.click(parameters)
  assert.equal(parameters.getAttribute('aria-expanded'), 'true')
  button.focus()
  await user.keyboard(' ')
  assert.equal(button.getAttribute('aria-expanded'), 'false')
  assert.equal(button.querySelector('.is-failed')?.getAttribute('title'), '读取失败原因')
  assert.ok(screen.getAllByText('等待审批').length)
  assert.ok(screen.getByText('迟到结果已归档'))
})
test('思考永远不可展开，支持最新正文预览、用量与错误', () => {
  const view = render(
    <AgentThinking
      call={{ id: 'thinking', ordinal: 2, status: 'running' }}
      text={'第一行\n最新一行\n'}
    />,
  )
  assert.ok(screen.getByText('正在思考'))
  assert.ok(screen.getByText('最新一行'))
  assert.equal(view.container.querySelector('button'), null)
  view.rerender(
    <AgentThinking
      call={{ id: 'thinking', ordinal: 2, status: 'completed', usage: { total_tokens: 1200 } }}
    />,
  )
  assert.ok(screen.getByText('思考 2 · 已完成'))
  assert.ok(screen.getByText('· 1.2k tokens'))
  view.rerender(
    <AgentThinking call={{ id: 'thinking', ordinal: 2, status: 'failed', error: '模型失败' }} />,
  )
  assert.ok(screen.getByText('模型失败'))
})

test('第三次调用触发分组时保留已手动展开的子行，并尊重之后手动收起分组', () => {
  const calls = [tool('threshold-a'), tool('threshold-b')]
  const view = render(<AgentActivityLog run={run(calls)} current />)
  fireEvent.click(row('threshold-a'))
  view.rerender(<AgentActivityLog run={run([...calls, tool('threshold-c')])} current />)
  const group = screen.getByRole('button', { name: /3 次调用/ })
  assert.equal(group.getAttribute('aria-expanded'), 'true')
  assert.equal(row('threshold-a').getAttribute('aria-expanded'), 'true')
  fireEvent.click(group)
  view.rerender(<AgentActivityLog run={run([...calls, tool('threshold-c')])} current />)
  assert.equal(group.getAttribute('aria-expanded'), 'false')
})

test('媒体Host没有模型或工具记录时，从真实DAG子任务显示活动行并展开参数与结果', () => {
  const value = run([])
  value.steps = [
    {
      id: 'step-uuid',
      key: 'image',
      ordinal: 0,
      planVersion: 1,
      title: '生成画面',
      intent: '猫咪伸懒腰，漫画风格',
      tools: ['text_to_image'],
      dependsOn: [],
      doneWhen: '生成完成',
      status: 'completed',
      summary: null,
      evidenceRefs: [],
      retries: 0,
    },
  ]
  value.media = {
    tier: 'preview',
    goalType: 'image',
    previewRunId: null,
    quoteCredits: 10,
    chargedCredits: 10,
    refundCredits: 0,
    completed: 1,
    total: 1,
    documentRevision: 2,
    cancelReason: null,
    children: [
      {
        stepId: 'image',
        title: '生成画面',
        retryCredits: 10,
        index: 0,
        status: 'completed',
        assetId: 'actual-asset',
        errorCode: null,
        targetNode: 'actual-node',
        attempt: 1,
      },
    ],
  }
  const original = structuredClone(value)
  render(<AgentActivityLog run={value} current />)
  const button = screen.getByRole('button', { name: /生成.*生成画面.*步骤 1.*已返回/ })
  assert.equal(button.getAttribute('aria-expanded'), 'false')
  assert.equal(screen.queryByText('正在思考'), null)
  fireEvent.click(button)
  fireEvent.click(screen.getByRole('button', { name: '展开参数' }))
  assert.ok(screen.getByText('猫咪伸懒腰，漫画风格'))
  assert.ok(document.querySelector('.next-agent-tool-fields')?.textContent?.includes('猫咪伸懒腰'))
  fireEvent.click(screen.getByRole('button', { name: '展开返回数据' }))
  assert.ok(screen.getByText(/actual-asset/))
  assert.deepEqual(value, original)
})

test('混合分组先显示待审批，失败摘要保留错误悬停信息', () => {
  const failed = { ...tool('group-failure', 'failed'), error: '读取被拒绝' }
  const mounted = render(
    <AgentActivityLog
      run={run([failed, tool('approval', 'waiting_approval'), tool('done')])}
      current
    />,
  )
  assert.ok(screen.getByRole('button', { name: /读取.*3 次调用.*等待审批/ }))
  mounted.rerender(
    <AgentActivityLog run={run([failed, tool('approval', 'completed'), tool('done')])} current />,
  )
  assert.equal(
    screen
      .getByRole('button', { name: /读取.*3 次调用.*失败/ })
      .querySelector('.next-agent-activity-status')
      ?.getAttribute('title'),
    '读取被拒绝',
  )
})
