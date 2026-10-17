import './dom.js'
import test, { afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { cleanup, fireEvent, render, screen, waitFor, within, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { FlowOverview } from '../apps/web/evaluation/FlowOverview.js'
import { EvaluationApp } from '../apps/web/evaluation/EvaluationApp.js'
import type { CaseDefinition } from '../apps/web/evaluation/api.js'

const cases: CaseDefinition[] = [
  {
    caseId: 'case-1',
    title: '取消后继续恢复计划',
    taskType: 'agent_flow',
    suites: ['L4'],
    input: {
      prompt: '做广告',
      params: {
        steps: [{ action: 'send', prompt: '做广告' }],
        expect: { afterContinue: { hasPlan: true } },
      },
    },
    checks: [],
  },
  {
    caseId: 'case-2',
    title: '生成中停止后继续',
    taskType: 'agent_flow',
    suites: ['L4'],
    input: { prompt: '继续', params: {} },
    checks: [],
  },
]
afterEach(cleanup)
function stubFetch(initialStatus: string, error = false, ungraded = false) {
  const original = globalThis.fetch
  let status = initialStatus
  let starts = 0
  globalThis.fetch = async (input, init) => {
    const path = String(input)
    if (init?.method === 'POST') {
      starts++
      return new Response(
        JSON.stringify(
          error ? { error: '队列不可用，请重试' } : { runId: 'launched', status: 'queued' },
        ),
        { status: error ? 503 : 202 },
      )
    }
    const complete = status === 'completed'
    const progress = {
      total: 2,
      completed: complete ? 2 : 1,
      failed: 0,
      cancelled: 0,
      timedOut: 0,
      running: 0,
      pending: complete ? 0 : 1,
    }
    const body = path.endsWith('/runs')
      ? { runs: [] }
      : path.endsWith('/datasets')
        ? { datasets: [{ datasetId: 'ds', versions: [{ version: 1 }] }] }
        : path.endsWith('/trials')
          ? {
              trials: cases.map((item, index) => ({
                trialId: `trial-${index}`,
                caseId: item.caseId,
                status: complete || index === 0 ? 'completed' : 'pending',
                qualityVerdict: complete || (index === 0 && !ungraded) ? 'pass' : null,
                weightedScore: complete || (index === 0 && !ungraded) ? 1 : null,
                grades: [],
                error: null,
              })),
            }
          : path.includes('/cases')
            ? { cases }
            : {
                status,
                progress,
                denominators: { pass: complete ? 2 : 1, determined: complete ? 2 : 1 },
                flow: null,
              }
    return new Response(JSON.stringify(body))
  }
  return {
    restore() {
      globalThis.fetch = original
    },
    starts: () => starts,
    finish() {
      status = 'completed'
    },
  }
}
function flow(onOpenRun: (id: string) => void) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <FlowOverview onOpenRun={onOpenRun} onQuality={() => {}} />
    </QueryClientProvider>,
  )
  return client
}
async function start(name: string | RegExp = '运行整包') {
  const button = screen.getByRole('button', { name }) as HTMLButtonElement
  await waitFor(() => assert.equal(button.disabled, false))
  fireEvent.click(button)
}
test('待跑→完成完整链路保持同一工作区，展开结果也不导航', async () => {
  const stub = stubFetch('running', false, true)
  const opened: string[] = []
  try {
    const client = flow((id) => opened.push(id))
    await start()
    await screen.findByRole('progressbar')
    const workspace = screen.getByRole('region', { name: '本次评测工作区' })
    assert.ok(within(workspace).getByText(/待跑 1 条/u))
    assert.equal(screen.getByRole('progressbar').getAttribute('aria-valuenow'), '1')
    const launchButton = screen.getByRole('button', {
      name: '本次评测进行中…',
    }) as HTMLButtonElement
    assert.ok(launchButton.disabled)
    fireEvent.click(launchButton)
    assert.equal(stub.starts(), 1)
    fireEvent.click(await screen.findByText('展开本次 2 道题的执行情况'))
    await within(workspace).findByRole('button', { name: '取消后继续恢复计划' })
    assert.ok(within(workspace).getByText('待跑', { exact: true }))
    assert.equal(screen.queryByText('失败定位'), null)
    assert.ok(within(workspace).getByText(/待判分 1/u))
    assert.equal(within(workspace).queryByText(/其他结果/u), null)
    stub.finish()
    await act(async () => {
      await client.invalidateQueries({ queryKey: ['eval-launch'] })
    })
    await screen.findByRole('heading', { name: '评测已完成' })
    await waitFor(() => assert.equal(within(workspace).queryByText('待跑', { exact: true }), null))
    assert.equal(screen.getByRole('region', { name: '本次评测工作区' }), workspace)
    assert.deepEqual(opened, [])
    assert.equal(screen.queryByRole('button', { name: '查看结果' }), null)
    assert.ok(within(workspace).getByText(/通过 2 条/u))
    assert.equal(
      (screen.getByRole('button', { name: '运行整包' }) as HTMLButtonElement).disabled,
      false,
    )
  } finally {
    stub.restore()
  }
})
test('用例标题在原页展开定义，结果按钮不会跳页', async () => {
  const stub = stubFetch('completed')
  try {
    flow(() => {
      assert.fail('本次用例结果不应导航')
    })
    await start()
    await screen.findByRole('heading', { name: '评测已完成' })
    fireEvent.click(await screen.findByText('展开本次 2 道题的结果'))
    fireEvent.click(
      within(screen.getByRole('region', { name: '本次评测工作区' })).getByRole('button', {
        name: '取消后继续恢复计划',
      }),
    )
    assert.ok(screen.getByRole('heading', { name: '怎样算通过' }))
    assert.ok(screen.getByRole('heading', { name: 'Agent 流程评测' }))
  } finally {
    stub.restore()
  }
})
test('启动失败保留套件选择和原页，可重试', async () => {
  const stub = stubFetch('failed', true)
  try {
    flow(() => {
      assert.fail('启动失败不应导航')
    })
    fireEvent.change(screen.getByLabelText('评测套件'), { target: { value: 'L4' } })
    await start('运行 L4')
    await screen.findByText('队列不可用，请重试')
    assert.equal((screen.getByLabelText('评测套件') as HTMLSelectElement).value, 'L4')
    assert.equal(
      (screen.getByRole('button', { name: '运行 L4' }) as HTMLButtonElement).disabled,
      false,
    )
  } finally {
    stub.restore()
  }
})
test('新建评测启动后保留表单与页面，不自动导航', async () => {
  const stub = stubFetch('running')
  try {
    render(<EvaluationApp />)
    fireEvent.click(screen.getByRole('button', { name: '新建评测' }))
    await screen.findByRole('option', { name: /ds/u })
    fireEvent.change(screen.getByLabelText('运行 ID'), { target: { value: 'my-run' } })
    fireEvent.click(screen.getByRole('button', { name: '预检并启动 Mock 运行' }))
    await screen.findByRole('progressbar')
    assert.ok(screen.getByRole('heading', { name: '新建评测' }))
    assert.equal((screen.getByLabelText('运行 ID') as HTMLInputElement).value, 'my-run')
    assert.ok(screen.getByRole('button', { name: '查看详情' }))
  } finally {
    stub.restore()
  }
})

test('新建 Agent 评测预览与启动只包含勾选层', async () => {
  const stub = stubFetch('running')
  const fetching = globalThis.fetch
  let payload: { suite: string; layers: string[] } | undefined
  globalThis.fetch = async (input, init) => {
    if (init?.method === 'POST') {
      payload = JSON.parse(String(init.body)) as typeof payload
    }
    return fetching(input, init)
  }
  try {
    render(<EvaluationApp />)
    fireEvent.click(screen.getByRole('button', { name: '新建评测' }))
    await screen.findByRole('option', { name: /ds/u })
    fireEvent.change(screen.getByLabelText('数据集'), { target: { value: '__agent_source__' } })
    await screen.findByText('将运行 2 道题（本地模拟模型）')
    fireEvent.click(screen.getByLabelText('整包 agent_flow'))
    assert.ok(screen.getByRole('button', { name: '启动所选 Agent 层级' }).hasAttribute('disabled'))
    fireEvent.click(screen.getByLabelText('L4 续跑 · 2 题'))
    assert.ok(screen.getByText('取消后继续恢复计划'))
    fireEvent.click(screen.getByRole('button', { name: '启动所选 Agent 层级' }))
    await screen.findByRole('progressbar')
    assert.equal(payload?.suite, 'agent_flow')
    assert.deepEqual(payload?.layers, ['L4'])
    assert.ok(screen.getByRole('heading', { name: '新建评测' }))
  } finally {
    stub.restore()
  }
})
