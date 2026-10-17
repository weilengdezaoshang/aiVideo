import './dom.js'
import test, { afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import {
  AgentSourceCases,
  CaseExplanation,
  CaseList,
  DatasetCases,
  RunCaseDefinition,
} from '../apps/web/evaluation/CaseBrowser.js'
import { expectationLines, failureReason } from '../apps/web/evaluation/caseCopy.js'
import type { CaseDefinition, TrialView } from '../apps/web/evaluation/api.js'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { readFileSync, readdirSync } from 'node:fs'

afterEach(cleanup)
const flow: CaseDefinition = {
  caseId: 'cancel-continue',
  title: '取消后继续恢复计划',
  taskType: 'agent_flow',
  suites: ['L4'],
  input: {
    prompt: '做广告',
    params: {
      steps: [
        { action: 'send', prompt: '做广告' },
        { action: 'resume', approved: false },
        { action: 'send', prompt: '继续' },
      ],
      expect: {
        route: 'handoff_planner',
        afterCancel: { newArtifacts: 0 },
        afterContinue: { hasPlan: true },
      },
      mocks: { imageProvider: 'stub' },
    },
  },
  checks: [],
}
const image: CaseDefinition = {
  caseId: 'cat-image',
  taskType: 'text_to_image',
  suites: ['smoke'],
  input: { prompt: '橘猫坐窗边看雨', params: {} },
  checks: [
    { id: 'has_cat', question: '图中是否有猫？', expected: true, required: true, weight: 2 },
  ],
}

test('说明卡翻译取消、继续、路由，原始 JSON 默认折叠', () => {
  const view = render(<CaseExplanation item={flow} />)
  assert.ok(screen.getByText('取消后，新增作品数为 0。'))
  assert.ok(screen.getByText('继续后，必须满足：再次展示计划。'))
  assert.ok(screen.getByText('必须：交给策划。'))
  assert.equal(view.container.querySelector('details')?.open, false)
  assert.equal(view.container.querySelectorAll('.eval-case-timeline li').length, 3)
})
test('文生图使用 prompt 标题并展示判分问题与预期', () => {
  render(<CaseExplanation item={image} />)
  assert.ok(screen.getByRole('heading', { name: image.input.prompt }))
  assert.ok(screen.getByText(/图中是否有猫？ — 预期：是/))
})
test('搜索及套件筛选能打开共用说明卡', () => {
  render(<CaseList cases={[flow, image]} />)
  fireEvent.change(screen.getByLabelText('覆盖层 / 测试套件'), { target: { value: 'L4' } })
  assert.equal(screen.queryByRole('button', { name: image.input.prompt }), null)
  fireEvent.click(screen.getByRole('button', { name: flow.title }))
  assert.ok(screen.getByRole('heading', { name: '怎样算通过' }))
  fireEvent.change(screen.getByLabelText('搜索用例'), { target: { value: '不存在' } })
  assert.ok(screen.getByText('没有匹配的用例，请调整搜索或筛选。'))
})
test('未知嵌套字段与值不会丢失', () => {
  assert.match(
    expectationLines({ afterContinue: { newFlag: { mystery: 'abc' } } }).join(''),
    /未知字段：newFlag.*未知字段：mystery.*abc/u,
  )
  assert.match(failureReason('unmapped'), /未知字段：unmapped/u)
})
test('所有实际 Agent 数据集的预期字段和枚举均可翻译', () => {
  for (const file of readdirSync('evals/datasets').filter((name) =>
    name.startsWith('agent-flow-'),
  )) {
    for (const line of readFileSync(`evals/datasets/${file}`, 'utf8').trim().split('\n')) {
      const item = JSON.parse(line) as CaseDefinition
      assert.ok(item.expect)
      assert.doesNotMatch(expectationLines(item.expect!).join(''), /未知字段/u, file)
    }
  }
})
function wrapper(children: React.ReactNode) {
  return (
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      {children}
    </QueryClientProvider>
  )
}
test('跑前预览仅显示前五条，查看全部后可浏览', async () => {
  const original = globalThis.fetch
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        cases: Array.from({ length: 6 }, (_, i) => ({
          ...flow,
          caseId: `case-${i}`,
          title: `测试题目${i}`,
        })),
      }),
    )
  try {
    render(wrapper(<DatasetCases datasetId="flow" preview />))
    await screen.findByText('测试题目4')
    assert.equal(screen.queryByText('测试题目5'), null)
    fireEvent.click(screen.getByRole('button', { name: '查看全部用例' }))
    assert.ok(screen.getByRole('button', { name: '测试题目5' }))
  } finally {
    globalThis.fetch = original
  }
})
test('运行反查读取运行快照并显示自动分和失败原码', async () => {
  const original = globalThis.fetch
  let requested = ''
  globalThis.fetch = async (input) => {
    requested = String(input)
    return new Response(JSON.stringify({ cases: [flow] }))
  }
  const trial = {
    caseId: flow.caseId,
    weightedScore: 0.5,
    grades: [{ gradeId: 'g', checkId: 'continue_has_plan', status: 'fail' }],
  } as TrialView
  try {
    render(wrapper(<RunCaseDefinition runId="old-run" trial={trial} />))
    await screen.findByText('自动分：0.5')
    assert.equal(requested, '/api/evals/v1/runs/old-run/cases')
    assert.ok(screen.getByText('继续后未展示计划'))
    assert.ok(screen.getByText(/原码：continue_has_plan/u))
  } finally {
    globalThis.fetch = original
  }
})

test('源题库按磁盘层级列题，L1 三题、L4 续跑题并打开说明', async () => {
  const cases = readdirSync('evals/datasets')
    .filter((name) => name.startsWith('agent-flow-'))
    .flatMap((file) =>
      readFileSync(`evals/datasets/${file}`, 'utf8')
        .trim()
        .split('\n')
        .map((line) => {
          const item = JSON.parse(line) as CaseDefinition
          return { ...item, input: { prompt: '', params: {} }, checks: [] }
        }),
    )
  const original = globalThis.fetch
  globalThis.fetch = async () => new Response(JSON.stringify({ cases }))
  try {
    render(wrapper(<AgentSourceCases />))
    fireEvent.click(await screen.findByRole('button', { name: 'L1 路由 · 3 题' }))
    assert.equal(screen.getAllByRole('row').length, 4)
    fireEvent.click(screen.getByRole('button', { name: 'L4 续跑 · 4 题' }))
    assert.equal(screen.getAllByRole('row').length, 5)
    fireEvent.click(screen.getByRole('button', { name: '取消后发送继续恢复计划' }))
    assert.ok(screen.getByRole('heading', { name: '怎样算通过' }))
    assert.equal(document.querySelector('.eval-case-detail details')?.hasAttribute('open'), false)
  } finally {
    globalThis.fetch = original
  }
})
