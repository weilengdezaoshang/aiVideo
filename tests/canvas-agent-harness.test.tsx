import './dom.js'
import test, { afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { AgentPanel } from '../apps/web/canvas/agent-panel.js'
import { createAgentStore } from '../apps/web/canvas/agent/store.js'
import type { AgentApi } from '../apps/web/canvas/agent/api.js'
import type { AgentRun, AgentStep, AgentStepStatus } from '../apps/web/canvas/agent/types.js'
import type { ChatMessage } from '../apps/web/canvas/state/doc-store.js'

afterEach(() => {
  cleanup()
  localStorage.clear()
})
const history: ChatMessage[] = []

function step(
  id: string,
  key: string,
  ordinal: number,
  status: AgentStepStatus,
  extra: Partial<AgentStep> = {},
) {
  return {
    id,
    planVersion: 2,
    key,
    ordinal,
    title: `步骤 ${key}`,
    intent: '读取画布',
    tools: ['read_canvas'],
    dependsOn: [],
    doneWhen: '已取得证据',
    status,
    summary: null,
    evidenceRefs: [],
    retries: 0,
    ...extra,
  }
}

const envelope = {
  evidenceId: 'E1',
  kind: 'tool',
  tool: 'read_canvas',
  toolVersion: 1,
  callId: 'c1',
  stepKey: 's2',
  trust: 'untrusted_canvas_data',
  source: { documentId: 'doc', revision: 8, assetIds: [] },
  producedAt: '2026-10-07T00:00:00Z',
  status: 'ok',
  result: {
    data: {},
    evidence: [
      {
        id: 'E1.f1',
        statement: '画布上有一张图片',
        path: 'items[0].kind',
        value: 'image',
        scope: 'complete',
      },
    ],
    coverage: { complete: true, reason: null, missing: null },
    next: [],
    truncated: false,
    omitted: { objectIds: [], fields: [] },
    page: null,
  },
}

const harnessRun: AgentRun = {
  id: 'run1',
  clientRequestId: 'request',
  status: 'completed',
  message: '盘点画布素材',
  modelId: 'deepseek',
  baseRevision: 8,
  reply: '画布上有一张图片 [E1.f1]',
  error: null,
  stage: 'done',
  plan: { goal: '盘点画布素材', version: 2, current: 's3' },
  waiting: null,
  steps: [
    { ...step('st-old', 's1', 1, 'obsolete'), planVersion: 1, title: '旧版第一稿步骤' },
    step('st-a', 's2', 1, 'completed', {
      summary: '读取完成',
      evidenceRefs: ['E1.f1'],
      retries: 1,
    }),
    step('st-b', 's3', 2, 'running'),
    step('st-c', 's4', 3, 'pending'),
    step('st-d', 's5', 4, 'blocked', { summary: '素材不可读' }),
    step('st-e', 's6', 5, 'obsolete', { title: '被替代的步骤' }),
  ],
  reflections: [
    {
      id: 'r1',
      round: 1,
      source: 'hard_check',
      verdict: 'revise_answer',
      issues: [],
      question: null,
      hardChecks: {
        passed: false,
        checks: [
          { id: 'H1', passed: true, issues: [] },
          {
            id: 'H4',
            passed: false,
            issues: [
              {
                check: 'H4',
                code: 'NUMBER_MISMATCH',
                detail: '断言中的数字 99 未见于证据',
                claim: 0,
                ref: 'E1.f1',
              },
            ],
          },
        ],
      },
      createdAt: '',
    },
    {
      id: 'r2',
      round: 2,
      source: 'model',
      verdict: 'ask_user',
      issues: [
        { kind: 'ungrounded', problem: '第二条断言没有证据支持', stepKey: 's2', evidenceRefs: [] },
      ],
      question: '你说的“全部素材”包含音频轨道吗？',
      hardChecks: null,
      createdAt: '',
    },
  ],
  modelCalls: [],
  toolCalls: [
    {
      id: 'c1',
      evidenceId: 'E1',
      kind: 'read',
      stepKey: 's2',
      name: 'read_canvas',
      status: 'completed',
      arguments: {},
      output: envelope,
    },
  ],
  createdAt: '',
  updatedAt: '',
}

function setup(initial: AgentRun) {
  const api: AgentApi = {
    models: async () => ({
      models: [{ id: 'deepseek', label: 'DeepSeek', provider: 'deepseek', model: 'deepseek-chat' }],
      defaultModelId: 'deepseek',
    }),
    snapshot: async () => ({ documentId: 'doc', sessionId: 's', cursor: 0, runs: [initial] }),
    create: async () => {
      throw new Error('not used')
    },
    get: async () => ({ run: initial }),
    stop: async () => ({ run: initial }),
  }
  const store = createAgentStore({
    documentId: 'doc',
    api,
    ensureSaved: async () => 8,
    eventSourceFactory: () => ({ addEventListener() {}, close() {} }),
  })
  render(<AgentPanel store={store} history={history} getSelection={() => []} />)
  let close!: () => void
  act(() => {
    close = store.start()
  })
  return { close }
}

test('计划清单只显示最新版本，覆盖每种步骤状态并标记当前步骤', async () => {
  const { close } = setup(harnessRun)
  try {
    await screen.findByText(/生成计划/)
    fireEvent.click(screen.getByLabelText('查看执行信息'))
    for (const [title, status] of [
      ['步骤 s2', '已完成 · 重试 1 次'],
      ['步骤 s3', '执行中'],
      ['步骤 s4', '待执行'],
      ['步骤 s5', '受阻'],
      ['被替代的步骤', '已废弃'],
    ]) {
      const row = screen.getByText(title).closest('li')!
      assert.ok(row.textContent?.includes(status))
    }
    assert.ok(screen.getByText('读取完成'))
    assert.equal(screen.getByRole('progressbar').getAttribute('value'), '1')
    assert.equal(screen.queryByText(/旧版第一稿步骤/), null)
    const current = document.querySelector('.next-agent-step[data-current="true"]')
    assert.ok(current)
    assert.match(current.textContent ?? '', /步骤 s3/)
  } finally {
    close()
  }
})

test('反思卡片逐项展示硬检验结果、语义问题与待澄清问题', async () => {
  const { close } = setup(harnessRun)
  try {
    await screen.findByText(/复盘 · 2 条/)
    assert.ok(screen.getByText('硬检验：回答需修订'))
    const passed = screen.getByText(/H1：通过/)
    assert.equal(passed.closest('li')?.getAttribute('data-passed'), 'true')
    const failed = screen.getByText(/H4：未通过/)
    assert.equal(failed.closest('li')?.getAttribute('data-passed'), 'false')
    assert.match(failed.textContent ?? '', /数字 99/)
    assert.ok(screen.getByText('模型复盘：需要用户澄清'))
    assert.ok(screen.getByText('缺乏依据（s2）：第二条断言没有证据支持'))
    assert.ok(screen.getByText('待澄清：你说的“全部素材”包含音频轨道吗？'))
  } finally {
    close()
  }
})

test('步骤证据引用与工具调用关联步骤标识', async () => {
  const { close } = setup(harnessRun)
  try {
    await screen.findByText(/生成计划/)
    fireEvent.click(screen.getByLabelText('查看执行信息'))
    const chips = screen.getAllByRole('link', { name: 'E1.f1' })
    assert.ok(chips.some((chip) => chip.getAttribute('href') === '#agent-fact-run1-E1.f1'))
    assert.ok(document.getElementById('agent-fact-run1-E1.f1'))
    const tool = screen.getByRole('button', { name: /读取.*read_canvas.*s2.*已返回/ })
    fireEvent.click(tool)
    assert.ok(screen.getByText('证据 E1'))
  } finally {
    close()
  }
})

test('waiting_input 显示待答问题且允许直接发送回答', async () => {
  const run: AgentRun = {
    ...harnessRun,
    id: 'run2',
    status: 'waiting_input',
    stage: 'waiting',
    reply: null,
    waiting: { question: '你要盘点哪一类素材？', stage: 'respond' },
  }
  const { close } = setup(run)
  try {
    await screen.findByText(/等待你的回答/)
    assert.ok(screen.getByText('你要盘点哪一类素材？'))
    const send = screen.getByRole('button', { name: '发送' }) as HTMLButtonElement
    fireEvent.change(screen.getByRole('textbox', { name: '向画布助手提问' }), {
      target: { value: '只看图片素材' },
    })
    assert.equal(send.disabled, false)
    assert.equal(screen.queryByRole('button', { name: '停止' }), null)
  } finally {
    close()
  }
})

test('没有 harness 字段的旧运行不渲染计划与复盘', async () => {
  const legacy: AgentRun = {
    id: 'run3',
    clientRequestId: 'old',
    status: 'completed',
    message: '旧版问题',
    modelId: 'deepseek',
    baseRevision: 3,
    reply: '旧版回答',
    error: null,
    modelCalls: [],
    toolCalls: [],
    createdAt: '',
    updatedAt: '',
  }
  const { close } = setup(legacy)
  try {
    await screen.findByText('旧版回答')
    assert.equal(screen.queryByText(/生成计划/), null)
    assert.equal(screen.queryByText(/复盘/), null)
    assert.equal(screen.queryByText(/需要你补充信息/), null)
  } finally {
    close()
  }
})

test('ZCode活动行默认可见，任务清单通过执行信息入口按需展开', async () => {
  const { close } = setup(harnessRun)
  try {
    const title = await screen.findByText(/生成计划/)
    const plan = title.closest('details')!
    assert.equal(plan.open, true)
    const diagnostics = plan.closest('.next-agent-info') as HTMLDetailsElement
    assert.ok(diagnostics)
    assert.equal(diagnostics.open, false)
    const process = screen.getByRole('region', { name: '处理过程' })
    assert.equal(process.tagName, 'SECTION')
    assert.ok(screen.getByRole('button', { name: /读取.*read_canvas.*s2.*已返回/ }))
    fireEvent.click(screen.getByLabelText('查看执行信息'))
    assert.equal(diagnostics.open, true)
    assert.ok(screen.getByText(/步骤 s2/))
  } finally {
    close()
  }
})
