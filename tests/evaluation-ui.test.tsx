// 评测工作台组件测试:总览加载态/空态、运行详情进度与取消、预算口径展示、
// 审核操作身份校验、比较口径(mock 不计独立样本)。fetch 打桩,不发真实请求。
import './dom.js'
import test from 'node:test'
import assert from 'node:assert/strict'
import { afterEach, describe } from 'node:test'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { EvaluationApp, STATUS_LABELS } from '../apps/web/evaluation/EvaluationApp.js'

afterEach(() => cleanup())

type Stub = { pattern: RegExp; body: unknown }

function installFetch(stubs: Stub[]) {
  const calls: string[] = []
  const original = globalThis.fetch
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = String(input)
    calls.push(url)
    const matched = stubs.find((stub) => stub.pattern.test(url))
    return new Response(JSON.stringify(matched ? matched.body : {}), {
      status: matched ? 200 : 404,
      headers: { 'content-type': 'application/json' },
    })
  }) as typeof fetch
  return {
    calls,
    restore: () => {
      globalThis.fetch = original
    },
  }
}

const DATASETS = {
  datasets: [
    {
      datasetId: 'ds-zh',
      versions: [
        {
          version: 1,
          split: 'dev',
          caseCount: 3,
          contentHash: 'abc123def456',
          frozenAt: '2026-09-01T00:00:00Z',
        },
      ],
    },
  ],
}

const RUNS = {
  runs: [
    {
      runId: 'run-live-1',
      createdAt: '2026-09-12T00:00:00Z',
      mode: 'live',
      provider: 'cloud',
      purpose: '验收',
      legacy: false,
      status: 'running',
      planCount: 2,
      planHash: 'aa11bb22',
    },
  ],
}

const RUN_DETAIL = {
  runId: 'run-live-1',
  manifest: {
    purpose: '验收',
    mode: 'live',
    provider: 'cloud',
    dataset: {
      datasetId: 'ds-zh',
      version: 1,
      contentHash: 'abc123def4567890',
      frozenAt: '2026-09-01T00:00:00Z',
    },
    planCount: 2,
    grading: { judge: 'stub' },
    legacy: false,
    parentRunId: null,
    recordingId: 'rec-run-live-1',
    compression: { enabled: false },
  },
  status: 'running',
  progress: {
    total: 2,
    pending: 1,
    running: 1,
    completed: 0,
    failed: 0,
    cancelled: 0,
    timedOut: 0,
  },
  extra: {
    replay: {
      matchProtocolVersion: 2,
      matched: 3,
      unconsumedInPlan: [1],
      unconsumedOutOfPlan: [],
      newExternalCalls: 2,
      newExternalCallDetail: [{ method: 'POST', url: 'http://telemetry.local/v1' }],
      verification: 'failed',
    },
  },
  denominators: { plan: 2, attempted: 1, generated: 1, determined: 1, pass: 1 },
  metrics: { executionCoverage: 0.5, conditionalQualityPassRate: 1.0, planCompletionRate: 0.0 },
  annotations: ['stub 判分:合成回答,仅验证判分管线'],
  coverageGaps: ['t2i-b(重复 0):未执行—未产生可评分素材'],
}

const TRIALS = {
  trials: [
    {
      trialId: 'trial-1',
      caseId: 't2i-a',
      repetitionIndex: 0,
      status: 'running',
      qualityVerdict: null,
      weightedScore: null,
      requestedSeed: 7,
      resolvedSeed: null,
      error: null,
      artifactIds: [],
      grades: [],
    },
  ],
}

describe('评测工作台', () => {
  test('状态中文映射覆盖新增终态(取消中/预算中止/中断)', () => {
    assert.equal(STATUS_LABELS.stopping, '取消中')
    assert.equal(STATUS_LABELS.budget_exhausted, '预算中止')
    assert.equal(STATUS_LABELS.interrupted, '已中断(待恢复)')
  })

  test('总览:数据集与运行列表渲染,空态有引导', async () => {
    const fetcher = installFetch([
      { pattern: /datasets$/, body: DATASETS },
      { pattern: /runs$/, body: { runs: [] } },
    ])
    render(<EvaluationApp />)
    await waitFor(() => screen.getByText('ds-zh'))
    assert.ok(screen.getByText(/尚无评测运行/))
    assert.ok(screen.getByText(/v1/))
    fetcher.restore()
  })

  test('运行详情:显示进度、取消按钮、回放证据与覆盖缺口', async () => {
    const fetcher = installFetch([
      { pattern: /runs\/run-live-1$/, body: RUN_DETAIL },
      { pattern: /runs\/run-live-1\/trials$/, body: TRIALS },
      { pattern: /runs\/run-live-1\/gates$/, body: { gates: [] } },
      { pattern: /runs$/, body: RUNS },
    ])
    render(<EvaluationApp />)
    fireEvent.click(screen.getByRole('button', { name: '运行详情' }))
    await waitFor(() => screen.getByText(/取消运行/))
    assert.ok(screen.getByText(/完成 0\/2/))
    assert.ok(screen.getByText(/回放证据/))
    assert.ok(screen.getByText(/新增外部调用 2/))
    assert.ok(document.querySelector('.eval-replay-evidence strong')?.textContent === '失败')
    assert.ok(screen.getByText(/覆盖缺口/))
    // 运行中才显示取消按钮
    const cancel = screen.getByRole('button', { name: '取消运行' })
    assert.ok(cancel)
    fetcher.restore()
  })

  test('取消按钮调用真实取消端点并刷新', async () => {
    const fetcher = installFetch([
      { pattern: /runs\/run-live-1$/, body: RUN_DETAIL },
      { pattern: /runs\/run-live-1\/trials$/, body: TRIALS },
      { pattern: /runs\/run-live-1\/gates$/, body: { gates: [] } },
      { pattern: /runs$/, body: RUNS },
      {
        pattern: /cancel$/,
        body: { runId: 'run-live-1', status: 'stopping', cancelRequested: true },
      },
    ])
    render(<EvaluationApp />)
    fireEvent.click(screen.getByRole('button', { name: '运行详情' }))
    await waitFor(() => screen.getByRole('button', { name: '取消运行' }))
    fireEvent.click(screen.getByRole('button', { name: '取消运行' }))
    await waitFor(() => {
      assert.ok(fetcher.calls.some((url) => url.endsWith('/cancel')))
    })
    fetcher.restore()
  })

  test('预算面板:上限/双口径结算/未结预留/未知,不把预留伪装成实际费用', async () => {
    const fetcher = installFetch([
      { pattern: /runs\/run-live-1$/, body: RUN_DETAIL },
      { pattern: /runs\/run-live-1\/trials$/, body: TRIALS },
      { pattern: /runs\/run-live-1\/gates$/, body: { gates: [] } },
      { pattern: /runs$/, body: RUNS },
      {
        pattern: /budget$/,
        body: {
          scopeId: 'run-live-1',
          settled: 0.55,
          settledBilled: 0.42,
          settledEstimated: 0.13,
          outstandingReserved: 0.25,
          unknownCount: 1,
          totalCalls: 4,
          currency: 'CNY',
          maxCost: 5.0,
          maxExternalCalls: null,
          priceVersion: '1',
          reservations: [
            {
              reservationId: 'res-1',
              callSite: 'generation',
              status: 'unknown',
              reservedAmount: 0.25,
              settledAmount: null,
              settledBasis: null,
              trialId: 'trial-1',
              note: '发送后异常:ConnectTimeout',
            },
          ],
        },
      },
    ])
    render(<EvaluationApp />)
    fireEvent.click(screen.getByRole('button', { name: '运行详情' }))
    fireEvent.click(await screen.findByRole('button', { name: '查看预算账本' }))
    await waitFor(() => screen.getByText('预算账本(预留 ≠ 实际费用)'))
    assert.ok(screen.getByText('5.0000 CNY'))
    assert.ok(screen.getByText(/0\.4200 CNY/)) // 账单口径单独列出
    assert.ok(screen.getByText('unknown'))
    assert.ok(screen.getByText(/发送后异常:ConnectTimeout/))
    fetcher.restore()
  })

  test('审核:无审核人时操作被拦截并提示身份要求', async () => {
    const fetcher = installFetch([
      {
        pattern: /review-tasks$/,
        body: {
          tasks: [
            {
              reviewTaskId: 'review-1',
              runId: 'run-live-1',
              trialId: 'trial-1',
              caseId: 't2i-a',
              status: 'pending',
              revision: 1,
              priority: 'required',
              claim: null,
              sampling: { rule: 'required=fail_or_undetermined', policyVersion: 'sampling-v1' },
            },
          ],
        },
      },
    ])
    render(<EvaluationApp />)
    fireEvent.click(screen.getByRole('button', { name: '人工审核' }))
    const claimButtons = await screen.findAllByRole('button', { name: '领取' })
    fireEvent.click(claimButtons[0])
    await waitFor(() => screen.getByText(/请先填写审核人身份/))
    fetcher.restore()
  })

  test('比较结果:mock 不计独立样本,总体差值显示 N/A', async () => {
    const fetcher = installFetch([
      {
        pattern: /comparisons$/,
        body: {
          comparability: 'compatible',
          note: '数据集快照与执行/评分配置一致,可进行配对比较',
          configDifferences: [],
          subjectDeltas: [],
          independentSamples: false,
          checkRegressions: [
            {
              caseId: 't2i-a',
              checkId: 'has_cat',
              baselineStatus: 'pass',
              candidateStatus: 'fail',
              baselineGradeId: 'trial-a:has_cat',
              candidateGradeId: 'trial-b:has_cat',
              candidateTrialId: 'trial-b',
            },
          ],
          candidateErrorCategories: [{ category: 'timeout', trialIds: ['trial-c'] }],
          coverage: { pairedCount: 2, onlyInBaseline: [], onlyInCandidate: [] },
          summary: {
            baselinePassRate: 1.0,
            candidatePassRate: 1.0,
            overallDelta: null,
            regressions: [],
            improvements: [],
            uncertainty: '证据不足:单次重复不做显著性结论(§12.4)',
          },
          paired: [],
        },
      },
    ])
    render(<EvaluationApp />)
    fireEvent.click(screen.getByRole('button', { name: '批次比较' }))
    const inputs = screen.getAllByRole('textbox')
    fireEvent.change(inputs[0], { target: { value: 'base' } })
    fireEvent.change(inputs[1], { target: { value: 'cand' } })
    fireEvent.click(screen.getByRole('button', { name: '比较' }))
    await waitFor(() => screen.getByText(/独立统计样本/))
    assert.ok(screen.getByText(/否\(Mock\/缓存\/回放不计入样本量\)/))
    assert.ok(screen.getByText(/N\/A/))
    // 退化定位:检查项级退化与错误类别分组可见,gradeId 可回查
    assert.ok(screen.getByText(/检查项级退化/))
    assert.ok(screen.getByText('has_cat'))
    assert.ok(screen.getByText('trial-b:has_cat'))
    assert.ok(screen.getByText(/按错误类别分组/))
    assert.ok(screen.getByText('timeout'))
    fetcher.restore()
  })

  test('运营:Langfuse 未配置如实显示,不显示已接通', async () => {
    const fetcher = installFetch([
      {
        pattern: /health$/,
        body: {
          activeRuns: 0,
          stuckTrials: [],
          unknownExternalTrials: 0,
          budget: { unsettledReservations: 0, unknownOutcomes: 0 },
          judgeErrorTrials: 0,
          judgedTrials: 5,
          oldestPendingReviewHours: null,
          storeBytes: 1024,
          note: 'ok',
        },
      },
      {
        pattern: /alerts$/,
        body: { rules: {}, alerts: [], note: 'ok' },
      },
      {
        pattern: /outbox\/status$/,
        body: { pending: 12, langfuseConfigured: false, note: '未配置' },
      },
      { pattern: /calibrations$/, body: { calibrations: [] } },
    ])
    render(<EvaluationApp />)
    fireEvent.click(screen.getByRole('button', { name: '运营状态' }))
    await screen.findByText('未配置', { exact: true })
    assert.ok(screen.getByText(/积压 12 条/))
    assert.ok(screen.getByText(/尚无校准记录/))
    fetcher.restore()
  })
})

describe('评测工作台·审核闭环与调用记录', () => {
  test('审核详情:展示素材、自动证据,并可冻结进回归集', async () => {
    const fetcher = installFetch([
      {
        pattern: /review-tasks$/,
        body: {
          tasks: [
            {
              reviewTaskId: 'review-1',
              runId: 'run-live-1',
              trialId: 'trial-1',
              caseId: 't2i-a',
              status: 'adjudicated',
              revision: 3,
              priority: 'required',
              claim: { reviewer: 'alice', leaseUntil: '2026-09-12T10:00:00Z' },
              sampling: { rule: 'required=fail_or_undetermined', policyVersion: 'sampling-v1' },
              artifactIds: ['art-abc123'],
            },
          ],
        },
      },
      {
        pattern: /review-tasks\/review-1$/,
        body: {
          task: {
            reviewTaskId: 'review-1',
            runId: 'run-live-1',
            trialId: 'trial-1',
            caseId: 't2i-a',
            status: 'adjudicated',
            revision: 3,
            priority: 'required',
            claim: { reviewer: 'alice', leaseUntil: '2026-09-12T10:00:00Z' },
            sampling: { rule: 'required=fail_or_undetermined', policyVersion: 'sampling-v1' },
            artifactIds: ['art-abc123'],
          },
          opinions: [
            {
              opinionId: 'op-1',
              reviewer: 'alice',
              revision: 2,
              verdicts: [{ checkId: 'has_cat', verdict: 'fail' }],
              agreeWithAuto: false,
              category: null,
              note: '猫眼数量不符',
              uncertain: false,
              supersededBy: null,
              submittedAt: '2026-09-12T01:00:00Z',
            },
          ],
          disputes: [],
          adjudications: [
            {
              adjudicationId: 'adj-1',
              boundOpinionIds: ['op-1'],
              finalVerdict: 'fail',
              reason: '人工确认失败',
              decidedBy: 'lead',
              rubricVersion: '1',
              at: '2026-09-12T01:10:00Z',
            },
          ],
        },
      },
      {
        pattern: /runs\/run-live-1\/trials$/,
        body: {
          trials: [
            {
              trialId: 'trial-1',
              caseId: 't2i-a',
              repetitionIndex: 0,
              status: 'completed',
              qualityVerdict: 'pass',
              weightedScore: 1,
              requestedSeed: 7,
              resolvedSeed: 7,
              error: null,
              artifactIds: ['art-abc123'],
              grades: [
                {
                  gradeId: 'trial-1:has_cat',
                  checkId: 'has_cat',
                  status: 'pass',
                  observed: true,
                  expected: true,
                  score: 1,
                  source: 'rules',
                },
              ],
            },
          ],
        },
      },
      { pattern: /case-drafts$/, body: { draftId: 'draft-1' } },
      {
        pattern: /publish$/,
        body: { datasetId: 'regression-zh', version: 3, contentHash: 'fedcba9876543210' },
      },
      // 素材内容端点:二进制,img 加载失败不影响断言
      { pattern: /artifacts\/.+\/content$/, body: {} },
    ])
    render(<EvaluationApp />)
    fireEvent.click(screen.getByRole('button', { name: '人工审核' }))
    fireEvent.click(await screen.findByRole('button', { name: '详情' }))
    await waitFor(() => screen.getByText(/自动证据/))
    assert.ok(screen.getByAltText('素材 art-abc123'))
    assert.ok(screen.getByText(/人工确认失败/))
    // 转回归用例:填写表单后提交,走 case-drafts → publish 两步
    const inputs = screen.getAllByRole('textbox')
    // 8 个 textbox = 审核表单 4 个 + 草稿表单 4 个(期望是 select 不计入)
    fireEvent.change(inputs[inputs.length - 4], { target: { value: '复现提示词' } })
    fireEvent.change(inputs[inputs.length - 3], { target: { value: 'regression-cat' } })
    fireEvent.change(inputs[inputs.length - 2], { target: { value: '图中是否出现完整猫脸?' } })
    fireEvent.change(inputs[inputs.length - 1], { target: { value: 'regression-zh' } })
    fireEvent.click(screen.getByRole('button', { name: '校验并冻结进回归集' }))
    await waitFor(() => screen.getByText(/regression-zh@v3/))
    assert.ok(fetcher.calls.some((url) => url.endsWith('/case-drafts')))
    assert.ok(fetcher.calls.some((url) => url.includes('/dataset-drafts/draft-1/publish')))
    fetcher.restore()
  })

  test('trial 详情:展示调用记录(调用点/状态/用量/费用)', async () => {
    installFetch([
      { pattern: /runs\/run-live-1$/, body: RUN_DETAIL },
      { pattern: /runs\/run-live-1\/trials$/, body: TRIALS },
      { pattern: /runs\/run-live-1\/gates$/, body: { gates: [] } },
      { pattern: /runs$/, body: RUNS },
      { pattern: /runs\/run-live-1\/events/, body: { events: [] } },
      {
        pattern: /trials\/trial-1$/,
        body: {
          runId: 'run-live-1',
          trial: TRIALS.trials[0],
          steps: [],
          grades: [],
          attempts: [
            {
              attemptId: 'trial-1-gen-0',
              trialId: 'trial-1',
              callSite: 'generation',
              attemptIndex: 0,
              status: 'succeeded',
              usage: null,
              cost: null,
              externalTaskId: 'task-9',
              error: null,
            },
          ],
          artifacts: [],
        },
      },
    ])
    render(<EvaluationApp />)
    fireEvent.click(screen.getByRole('button', { name: '运行详情' }))
    fireEvent.click(await screen.findByText('t2i-a')) // 选中 trial
    await screen.findByText('generation') // 等调用记录数据到达(加载态文案不含 generation)
    assert.ok(screen.getByText('task-9'))
  })
})
