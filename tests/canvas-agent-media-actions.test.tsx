import './dom.js'
import test, { afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { AgentMediaActions } from '../apps/web/canvas/agent-media-actions.js'
import { createAgentStore } from '../apps/web/canvas/agent/store.js'
import type { AgentRun } from '../apps/web/canvas/agent/types.js'
afterEach(cleanup)
const run: AgentRun = {
  id: 'failed-final',
  clientRequestId: 'r',
  message: '生成图片',
  status: 'failed',
  reply: null,
  error: '部分任务未完成',
  modelId: 'host',
  baseRevision: 1,
  modelCalls: [],
  toolCalls: [],
  createdAt: '',
  updatedAt: '',
  media: {
    tier: 'final',
    goalType: 'image',
    previewRunId: 'preview',
    quoteCredits: 20,
    chargedCredits: 0,
    refundCredits: 0,
    completed: 1,
    total: 2,
    documentRevision: 1,
    cancelReason: null,
    children: [
      {
        stepId: 'image',
        title: '窗边的猫',
        retryCredits: 20,
        index: 0,
        status: 'failed',
        assetId: null,
        errorCode: 'UPSTREAM_UNAVAILABLE',
        targetNode: 'node',
        attempt: 1,
      },
    ],
  },
}
test('失败任务就地展示原因与费用，重试等待时禁止再次提交', async () => {
  let resolve!: () => void
  const calls: string[] = []
  const store = {
    ...createAgentStore({ documentId: 'test', ensureSaved: async () => 1 }),
    retry: async (id: string, step: string, index: number) => {
      calls.push(`${id}:${step}:${index}`)
      await new Promise<void>((done) => {
        resolve = done
      })
    },
  }
  render(<AgentMediaActions run={run} store={store} sending={false} />)
  assert.ok(screen.getByText('正片生成未完成'))
  assert.ok(screen.getByText('已完成的 1 个作品保留在画布中。'))
  assert.ok(screen.getByText('暂时无法连接生成服务，请稍后重试。'))
  assert.ok(screen.getByText('20 积分起'))
  fireEvent.click(screen.getByRole('button', { name: '重试生成' }))
  const pending = screen.getByRole('button', { name: '正在提交…' }) as HTMLButtonElement
  assert.equal(pending.disabled, true)
  fireEvent.click(pending)
  assert.deepEqual(calls, ['failed-final:image:0'])
  resolve()
  await waitFor(() =>
    assert.equal(
      (screen.getByRole('button', { name: '重试生成' }) as HTMLButtonElement).disabled,
      false,
    ),
  )
})
test('存在后续正片任务时不再展示生成入口', () => {
  const store = createAgentStore({ documentId: 'test', ensureSaved: async () => 1 })
  render(
    <AgentMediaActions
      run={{ ...run, status: 'awaiting_user_final' }}
      store={store}
      sending={false}
      finalRun={{ ...run, status: 'queued' }}
    />,
  )
  assert.equal(screen.queryByRole('button', { name: '生成正片' }), null)
  assert.ok(screen.getByRole('status').textContent?.includes('正片任务已创建'))
})
