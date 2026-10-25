/** 会话面板交互渲染：问题卡/表单/费用/取消与预算按钮。 */
import './dom.js'
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach } from 'node:test'
import React from 'react'
import { SessionPanel } from '../apps/web/canvas/session/panel.js'
import type { RunCosts, SessionSnapshot } from '../apps/web/canvas/session/store.js'

afterEach(() => cleanup())

type StoreOverrides = {
  snapshot?: SessionSnapshot | null
  connected?: boolean
}

function makeStore(overrides: StoreOverrides = {}) {
  const calls: { method: string; args: unknown[] }[] = []
  const snapshot: SessionSnapshot = overrides.snapshot ?? {
    id: 's1',
    cursor: 1,
    summary: '',
    messages: [],
    runs: [],
  }
  // useSyncExternalStore 要求 getSnapshot 返回稳定引用
  const state = {
    snapshot,
    connected: overrides.connected ?? true,
    pending: [] as unknown[],
    error: '',
  }
  return {
    calls,
    subscribe: () => () => undefined,
    getSnapshot: () => state,
    send: async () => {
      calls.push({ method: 'send', args: [] })
    },
    retry: async () => undefined,
    markRead: async () => undefined,
    answer: async (taskId: string, optionId?: string, fields?: Record<string, unknown>) => {
      calls.push({ method: 'answer', args: [taskId, optionId, fields] })
    },
    budget: async (runId: string, action: string, additional?: number) => {
      calls.push({ method: 'budget', args: [runId, action, additional] })
    },
    cancelRun: async (runId: string) => {
      calls.push({ method: 'cancelRun', args: [runId] })
    },
    costs: async (runId: string): Promise<RunCosts> => {
      calls.push({ method: 'costs', args: [runId] })
      return {
        run_id: runId,
        credits_spent: 10,
        tasks: [
          {
            task_id: 'a',
            step: 't1',
            status: 'succeeded',
            reserved: 10,
            actual: 10,
            refunded: 0,
            settlement: 'settled',
            retry_count: 0,
          },
          {
            task_id: 'b',
            step: 't2',
            status: 'failed',
            reserved: 10,
            actual: 0,
            refunded: 10,
            settlement: 'refunded',
            retry_count: 1,
          },
        ],
      }
    },
  }
}

function snapshotWithRun(run: Record<string, unknown>): SessionSnapshot {
  return { id: 's1', cursor: 2, summary: '', messages: [], runs: [run as never] }
}

test('等待选项渲染为按钮并调用 answer', async () => {
  const store = makeStore({
    snapshot: snapshotWithRun({
      id: 'r1',
      status: 'paused',
      pause_reason: 'waiting_user',
      tasks: [
        {
          id: 'q1',
          key: 't2',
          kind: 'ask_user',
          status: 'waiting_user',
          question: {
            question: '请选择要抠出的主体。',
            options: [
              { id: '0', label: '杯子' },
              { id: 'all', label: '全部' },
            ],
          },
        },
      ],
    }),
  })
  render(<SessionPanel store={store as never} selectedAssets={() => []} />)
  const option = await screen.findByRole('button', { name: '杯子' })
  option.click()
  const call = store.calls.find((item) => item.method === 'answer')
  assert.ok(call, 'answer 应被调用')
  assert.deepEqual(call?.args, ['q1', '0', undefined])
})

test('表单渲染默认值与必填并提交 fields', async () => {
  const store = makeStore({
    snapshot: snapshotWithRun({
      id: 'r1',
      status: 'paused',
      pause_reason: 'waiting_user',
      tasks: [
        {
          id: 'q2',
          key: 't3',
          kind: 'ask_user',
          status: 'waiting_user',
          question: {
            form: {
              id: 'clarify',
              fields: [
                { id: 'detail', label: '补充细节', required: true, type: 'text', default: '黄昏' },
                { id: 'count', label: '张数', type: 'number', default: 2 },
                { id: 'style', label: '风格', type: 'choice', choices: ['水彩', '厚涂'] },
              ],
            },
          },
        },
      ],
    }),
  })
  render(<SessionPanel store={store as never} selectedAssets={() => []} />)
  const detail = await screen.findByLabelText(/补充细节（必填）/)
  assert.equal((detail as HTMLInputElement).value, '黄昏')
  screen.getByRole('button', { name: '提交' }).click()
  const call = store.calls.find((item) => item.method === 'answer')
  assert.ok(call, 'answer 应被调用')
  const fields = call?.args?.[2] as Record<string, string>
  assert.equal(fields.detail, '黄昏')
  assert.equal(fields.count, '2')
})

test('超预算渲染追加与结束按钮并调用 budget', async () => {
  const store = makeStore({
    snapshot: snapshotWithRun({
      id: 'r9',
      status: 'paused',
      pause_reason: 'over_budget',
      credits_limit: 5,
      credits_spent: 5,
      tasks: [],
    }),
  })
  render(<SessionPanel store={store as never} selectedAssets={() => []} />)
  const input = await screen.findByLabelText('追加积分')
  fireEvent.change(input, { target: { value: '20' } })
  fireEvent.click(screen.getByRole('button', { name: '追加积分并继续' }))
  const call = store.calls.find((item) => item.method === 'budget')
  assert.ok(call, 'budget 应被调用')
  assert.equal(call?.args?.[0], 'r9')
  assert.equal(call?.args?.[1], 'extend')
})

test('费用明细展开展示每步花费与合计', async () => {
  const store = makeStore({
    snapshot: snapshotWithRun({ id: 'r2', status: 'completed', credits_spent: 10, tasks: [] }),
  })
  render(<SessionPanel store={store as never} selectedAssets={() => []} />)
  const summary = await screen.findByText(/花费明细/)
  summary.dispatchEvent(new Event('click', { bubbles: true }))
  const row = await screen.findByText('t1')
  assert.ok(row)
  assert.ok(screen.getByText('t2'))
})

test('活动运行渲染停止按钮并调用 cancelRun', async () => {
  const store = makeStore({
    snapshot: snapshotWithRun({
      id: 'r3',
      status: 'running',
      tasks: [
        {
          id: 'x',
          key: 't1',
          kind: 'call_tool',
          status: 'submitted',
          tool: 'text_to_image_preview',
          quoted: 10,
        },
      ],
    }),
  })
  render(<SessionPanel store={store as never} selectedAssets={() => []} />)
  const stop = await screen.findByRole('button', { name: '停止本次运行' })
  stop.click()
  const call = store.calls.find((item) => item.method === 'cancelRun')
  assert.deepEqual(call?.args, ['r3'])
})
