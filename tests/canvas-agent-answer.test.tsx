import './dom.js'
import test, { afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { AgentPanel } from '../apps/web/canvas/agent-panel.js'
import { createAgentStore } from '../apps/web/canvas/agent/store.js'
import type { AgentApi } from '../apps/web/canvas/agent/api.js'
import type { AgentClaimsPayload, AgentRun } from '../apps/web/canvas/agent/types.js'

afterEach(cleanup)

type Case = { name: string; value: unknown }
const cases = JSON.parse(
  readFileSync(new URL('./fixtures/agent-contracts/cases.json', import.meta.url), 'utf8'),
) as Case[]
const okEnvelope = cases.find((item) => item.name === 'ok envelope')!.value

const passed: AgentClaimsPayload = {
  answer: '',
  claims: [],
  verification: { passed: true, checks: [] },
}
const failedChecks: AgentClaimsPayload = {
  answer: '',
  claims: [],
  verification: {
    passed: false,
    checks: [
      {
        id: 'H4',
        passed: false,
        issues: [
          {
            check: 'H4',
            code: 'NUMBER_NOT_IN_EVIDENCE',
            detail: '断言中的数字 99 没有出现在所引证据中',
            claim: 0,
            ref: null,
          },
        ],
      },
      { id: 'H1', passed: true, issues: [] },
    ],
  },
}

function run(overrides: Partial<AgentRun>): AgentRun {
  return {
    id: 'run1',
    clientRequestId: 'request',
    status: 'completed',
    message: '有多少图片',
    modelId: 'deepseek',
    baseRevision: 7,
    reply: null,
    error: null,
    modelCalls: [],
    createdAt: '',
    updatedAt: '',
    toolCalls: [
      {
        id: 'call1',
        evidenceId: 'E2',
        kind: 'read',
        name: 'list_canvas_elements',
        status: 'completed',
        arguments: {},
        output: okEnvelope,
      },
      {
        id: 'call2',
        evidenceId: null,
        kind: 'control',
        name: 'finish',
        status: 'failed',
        arguments: {},
        output: {
          kind: 'control',
          tool: 'finish',
          callId: 'f1',
          status: 'rejected',
          effect: null,
          error: {
            code: 'CLAIMS_INVALID',
            category: 'invalid_arguments',
            message: '回答未通过证据检验：H4 断言中的数字 99 没有出现在所引证据中',
            retryable: true,
            details: { fields: [], unknownObjectIds: [], allowedTools: [], limit: null },
            next: [],
          },
        },
      },
    ],
    ...overrides,
  }
}

function mount(value: AgentRun) {
  const api: AgentApi = {
    models: async () => ({ models: [], defaultModelId: null }),
    snapshot: async () => ({ documentId: 'doc', sessionId: 's', cursor: 0, runs: [value] }),
    create: async () => ({ run: value }),
    get: async () => ({ run: value }),
    stop: async () => ({ run: value }),
  }
  const store = createAgentStore({
    documentId: 'doc',
    api,
    ensureSaved: async () => 1,
    eventSourceFactory: () => ({ addEventListener() {}, close() {} }),
  })
  const view = render(<AgentPanel store={store} history={[]} getSelection={() => []} />)
  const close = store.start()
  return { view, close }
}

test('回答中的引用是可跳转的标签，点击展开并定位对应事实', async () => {
  const { view, close } = mount(
    run({ reply: '共 63 张图片[E2.f1]，且未知引用[E9.f1]。', claims: passed }),
  )
  try {
    const chip = await screen.findByRole('link', { name: 'E2.f1' })
    assert.equal(chip.getAttribute('href'), '#agent-fact-run1-E2.f1')
    const target = view.container.querySelector('[id="agent-fact-run1-E2.f1"]')
    assert.ok(target)
    const row = target.closest('.next-agent-activity')?.querySelector('button')
    assert.equal(row?.getAttribute('aria-expanded'), 'false')
    fireEvent.click(chip)
    assert.equal(row?.getAttribute('aria-expanded'), 'true')
    const unknown = screen.getByText('E9.f1')
    assert.equal(unknown.className.includes('is-unknown'), true)
    assert.equal(screen.queryByRole('link', { name: 'E9.f1' }), null)
  } finally {
    close()
  }
})

test('通过检验的回答显示通过徽标，未通过的草稿显示失败项', async () => {
  const pass = mount(run({ reply: '共 63 张[E2.f1]', claims: passed }))
  try {
    await screen.findByText('已通过证据检验')
  } finally {
    pass.close()
    cleanup()
  }
  const fail = mount(
    run({
      status: 'limit_reached',
      reply: '共 99 张',
      claims: failedChecks,
      errorCode: 'ANSWER_UNVERIFIED',
    }),
  )
  try {
    await screen.findByText('未通过证据检验，以下回答仅为草稿')
    assert.match(screen.getByText(/H4：断言中的数字 99/).textContent ?? '', /99/)
  } finally {
    fail.close()
  }
})

test('没有检验记录的旧回答不显示徽标，控制回执显示拒绝原因', async () => {
  const { view, close } = mount(run({ reply: '旧回答', claims: null }))
  try {
    await screen.findByText('旧回答')
    assert.equal(view.container.querySelector('.next-agent-verdict'), null)
    assert.ok(
      Array.from(view.container.querySelectorAll('.next-agent-activity-row')).some((summary) =>
        /finish\s*失败/.test(summary.textContent ?? ''),
      ),
    )
    assert.match(screen.getByText(/回答未通过证据检验/).textContent ?? '', /CLAIMS_INVALID/)
  } finally {
    close()
  }
})

test('面板保留运行冻结的人格名称，旧运行省略重复助手标题', async () => {
  const named = mount(
    run({ reply: '好', persona: { id: 'warm', version: 2, displayName: '小帧' } }),
  )
  try {
    await screen.findByText('小帧')
    assert.equal(screen.queryByText('画布助手'), null)
  } finally {
    named.close()
    cleanup()
  }
  const legacy = mount(run({ reply: '好', persona: null }))
  try {
    await screen.findByText('好')
    assert.equal(screen.queryByText('画布助手'), null)
  } finally {
    legacy.close()
  }
})
