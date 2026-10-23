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

type CapturedRun = {
  mode?: string
  provider?: string
  judge?: string
  judgeConfig?: Record<string, unknown>
  sandboxConfig?: Record<string, string>
  budget?: Record<string, unknown>
}

function installPlannerFetch(
  routes: { id: string; label: string; provider: string; model: string }[],
) {
  const original = globalThis.fetch
  const bodies: CapturedRun[] = []
  const rawBodies: string[] = []
  globalThis.fetch = async (input, init) => {
    const path = String(input)
    if (init?.method === 'POST' && path.endsWith('/runs')) {
      const text = String(init.body)
      rawBodies.push(text)
      bodies.push(JSON.parse(text) as CapturedRun)
      return new Response(JSON.stringify({ runId: 'launched', status: 'queued' }), { status: 202 })
    }
    if (path.endsWith('/planner/routes')) {
      return new Response(JSON.stringify({ routes }))
    }
    if (path.endsWith('/datasets')) {
      return new Response(
        JSON.stringify({
          datasets: [
            { datasetId: 'media-ds', versions: [{ version: 1 }] },
            { datasetId: 'planner-ds', versions: [{ version: 2 }] },
          ],
        }),
      )
    }
    if (path.includes('/datasets/') && path.includes('/cases')) {
      const planner = path.includes('planner-ds')
      return new Response(
        JSON.stringify({
          cases: [
            {
              caseId: planner ? 'plan-1' : 'img-1',
              taskType: planner ? 'planner' : 'text_to_image',
              suites: [],
              input: { prompt: '画一张', params: {} },
              checks: [],
            },
          ],
        }),
      )
    }
    if (path.endsWith('/runs')) {
      return new Response(JSON.stringify({ runs: [] }))
    }
    return new Response(
      JSON.stringify({
        status: 'running',
        progress: {
          total: 1,
          completed: 0,
          failed: 0,
          cancelled: 0,
          timedOut: 0,
          running: 1,
          pending: 0,
        },
        denominators: { pass: 0, determined: 0 },
        flow: null,
      }),
    )
  }
  return {
    restore() {
      globalThis.fetch = original
    },
    bodies,
    rawBodies,
  }
}

test('媒体数据集创建不带规划器字段', async () => {
  const stub = installPlannerFetch([
    { id: 'route-a', label: '深', provider: 'deepseek', model: 'deepseek-chat' },
  ])
  try {
    render(<EvaluationApp />)
    fireEvent.click(screen.getByRole('button', { name: '新建评测' }))
    await screen.findByRole('option', { name: /media-ds/u })
    fireEvent.change(screen.getByLabelText('运行 ID'), { target: { value: 'media-run' } })
    await screen.findByRole('heading', { name: '将运行哪些题' })
    assert.equal(screen.queryByLabelText('规划器'), null)
    assert.ok(screen.getByText(/live 模式/u))
    fireEvent.click(screen.getByRole('button', { name: '预检并启动 Mock 运行' }))
    await screen.findByRole('progressbar')
    assert.equal(stub.bodies[0]?.sandboxConfig, undefined)
    assert.equal(stub.bodies[0]?.mode, 'mock')
    assert.equal(stub.bodies[0]?.provider, undefined)
  } finally {
    stub.restore()
  }
})

test('planner 数据集分别提交 Rule 与 LLM route，不传密钥或 URL', async () => {
  const stub = installPlannerFetch([
    { id: 'route-a', label: '深', provider: 'deepseek', model: 'deepseek-chat' },
  ])
  try {
    render(<EvaluationApp />)
    fireEvent.click(screen.getByRole('button', { name: '新建评测' }))
    await screen.findByRole('option', { name: /media-ds/u })
    fireEvent.change(screen.getByLabelText('数据集'), { target: { value: 'planner-ds' } })
    fireEvent.change(screen.getByLabelText('运行 ID'), { target: { value: 'plan-run' } })
    await screen.findByLabelText('规划器')
    assert.equal(screen.queryByText(/live 模式/u), null)
    assert.equal(screen.queryByLabelText('裁判模型'), null)
    fireEvent.change(screen.getByLabelText('预算上限'), { target: { value: '3' } })
    fireEvent.click(screen.getByRole('button', { name: '预检并启动 Rule 基线' }))
    await screen.findByRole('progressbar')
    assert.deepEqual(stub.bodies[0]?.sandboxConfig, { plannerMode: 'rule' })
    assert.equal(stub.bodies[0]?.mode, 'mock')
    assert.equal(stub.bodies[0]?.provider, 'mock')
    assert.equal(stub.bodies[0]?.judge, 'stub')
    assert.equal(stub.bodies[0]?.judgeConfig, undefined)
    assert.deepEqual(stub.bodies[0]?.budget, { currency: 'CNY', maxCost: 3, prices: {} })
    const raw = stub.rawBodies.join('\n')
    assert.equal(raw.includes('apiKey'), false)
    assert.equal(raw.includes('credentialRef'), false)
    assert.equal(raw.includes('http'), false)
    assert.equal(raw.includes('baseUrl'), false)
    assert.equal(raw.includes('plannerRouteId'), false)
  } finally {
    stub.restore()
  }
})

test('planner LLM 提交部署 route id，不传密钥或 URL', async () => {
  const stub = installPlannerFetch([
    { id: 'route-a', label: '深', provider: 'deepseek', model: 'deepseek-chat' },
  ])
  try {
    render(<EvaluationApp />)
    fireEvent.click(screen.getByRole('button', { name: '新建评测' }))
    await screen.findByRole('option', { name: /media-ds/u })
    fireEvent.change(screen.getByLabelText('数据集'), { target: { value: 'planner-ds' } })
    fireEvent.change(screen.getByLabelText('运行 ID'), { target: { value: 'plan-llm' } })
    await screen.findByLabelText('规划器')
    fireEvent.change(screen.getByLabelText('规划器'), { target: { value: 'llm' } })
    await screen.findByLabelText('部署 route')
    fireEvent.click(screen.getByRole('button', { name: '预检并启动 LLM 规划评测' }))
    await screen.findByRole('progressbar')
    assert.deepEqual(stub.bodies[0]?.sandboxConfig, {
      plannerMode: 'llm',
      plannerRouteId: 'route-a',
    })
    assert.equal(stub.bodies[0]?.provider, 'mock')
    assert.equal(stub.bodies[0]?.judge, 'stub')
    const raw = stub.rawBodies.join('\n')
    assert.equal(raw.includes('apiKey'), false)
    assert.equal(raw.includes('credentialRef'), false)
    assert.equal(raw.includes('http'), false)
    assert.equal(raw.includes('baseUrl'), false)
  } finally {
    stub.restore()
  }
})

test('未配置部署路由时不能把 LLM 规划评测当成已启动', async () => {
  const stub = installPlannerFetch([])
  try {
    render(<EvaluationApp />)
    fireEvent.click(screen.getByRole('button', { name: '新建评测' }))
    await screen.findByRole('option', { name: /planner-ds/u })
    fireEvent.change(screen.getByLabelText('数据集'), { target: { value: 'planner-ds' } })
    fireEvent.change(screen.getByLabelText('运行 ID'), { target: { value: 'plan-empty' } })
    await screen.findByText(/未配置部署路由/u)
    fireEvent.change(screen.getByLabelText('规划器'), { target: { value: 'llm' } })
    const button = screen.getByRole('button', {
      name: '预检并启动 LLM 规划评测',
    }) as HTMLButtonElement
    assert.equal(button.disabled, true)
    fireEvent.click(button)
    assert.equal(stub.bodies.length, 0)
  } finally {
    stub.restore()
  }
})
