// 成本与压缩配对交互:validate 先行、非法配对出「无法对比」卡、运行详情一键配对与重跑引导。
// fetch 打桩,不发真实请求;断言不出现「全未知成功态」报告。
import './dom.js'
import test from 'node:test'
import assert from 'node:assert/strict'
import { afterEach } from 'node:test'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { EvaluationApp } from '../apps/web/evaluation/EvaluationApp.js'

afterEach(cleanup)

type Stub = { pattern: RegExp; body: unknown; status?: number }

function installFetch(stubs: Stub[]) {
  const calls: string[] = []
  const original = globalThis.fetch
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = String(input)
    calls.push(url)
    const matched = stubs.find((stub) => stub.pattern.test(url))
    return new Response(JSON.stringify(matched ? matched.body : {}), {
      status: matched?.status ?? (matched ? 200 : 404),
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
          caseCount: 2,
          contentHash: 'hash-v1',
          frozenAt: '2026-09-01T00:00:00Z',
        },
      ],
    },
  ],
}

const RUNS = {
  runs: [
    {
      runId: 'base-run',
      createdAt: '2026-09-10T00:00:00Z',
      mode: 'live',
      provider: 'cloud',
      purpose: '基线',
      legacy: false,
      status: 'completed',
      planCount: 2,
      planHash: 'aa11',
      datasetId: 'ds-zh',
      compression: false,
    },
    {
      runId: 'comp-run',
      createdAt: '2026-09-12T00:00:00Z',
      mode: 'live',
      provider: 'cloud',
      purpose: '压缩',
      legacy: false,
      status: 'completed',
      planCount: 2,
      planHash: 'bb22',
      datasetId: 'ds-zh',
      compression: true,
    },
  ],
}

const PAIR_FOUND = {
  currentRunId: 'comp-run',
  currentRole: 'compressed',
  pairRunId: 'base-run',
  candidates: [],
  reasons: [],
  guidance: null,
  coverage: { alignedCases: 2, alignedCompletedCases: 2, baselinePlan: 2, compressedPlan: 2 },
}

const VALIDATED = {
  valid: true,
  baselineRunId: 'base-run',
  candidateRunId: 'comp-run',
  coverage: { alignedCases: 2, alignedCompletedCases: 2, baselinePlan: 2, compressedPlan: 2 },
}

const COMPARISON = {
  comparability: 'compatible',
  note: '数据集快照与执行/评分配置一致,可进行配对比较',
  configDifferences: [],
  subjectDeltas: [],
  independentSamples: true,
  checkRegressions: [],
  candidateErrorCategories: [],
  coverage: { pairedCount: 2, onlyInBaseline: [], onlyInCandidate: [] },
  summary: {
    baselinePassRate: 1.0,
    candidatePassRate: 0.95,
    overallDelta: -0.05,
    regressions: [],
    improvements: [],
    uncertainty: '每例 1 次重复;差值以用例为聚类单位',
  },
  paired: [
    {
      caseId: 't2i-a',
      baseline: { status: 'completed', verdict: 'pass', score: 1 },
      candidate: { status: 'completed', verdict: 'pass', score: 0.9 },
      delta: -0.1,
    },
    {
      caseId: 't2i-b',
      baseline: { status: 'completed', verdict: 'pass', score: 1 },
      candidate: { status: 'completed', verdict: 'pass', score: 1 },
      delta: 0,
    },
  ],
}

const COMPRESSION = {
  baselineRunId: 'base-run',
  compressedRunId: 'comp-run',
  compressorVersion: 'whitespace-v1',
  compressionEnabled: true,
  quality: {
    baselinePassRate: 1.0,
    compressedPassRate: 0.95,
    delta: -0.05,
    note: '通过率只计确定判定',
  },
  tokens: {
    baseline: { providerReportedTokens: 1000 },
    compressed: { providerReportedTokens: 700 },
    delta: -300,
    note: 'token 仅统计服务商 reported usage',
  },
  cost: { baselineSettled: 1.0, compressedSettled: 0.6, delta: -0.4, note: '已结算口径(估算)' },
  promote: true,
  promoteReason: '质量变化在允许范围内;推广仍需成本与覆盖证据支持',
}

test('成本页默认推荐对:先 validate 再生成,报告有真实数字与涨跌着色', async () => {
  const fetcher = installFetch([
    { pattern: /suggest-pair/, body: PAIR_FOUND },
    { pattern: /validate-pair$/, body: VALIDATED },
    { pattern: /comparisons$/, body: COMPARISON },
    { pattern: /compression-report$/, body: COMPRESSION },
    { pattern: /gates$/, body: { gates: [] } },
    { pattern: /runs$/, body: RUNS },
  ])
  try {
    render(<EvaluationApp />)
    fireEvent.click(screen.getByRole('button', { name: '成本与压缩' }))
    // 自动链路为 suggest-pair → validate → 比较/压缩/门禁,链路较长,放宽轮询上限
    // 压缩后通过率下降 5 个百分点 → 结论为「发现变差项」
    await screen.findByText('压缩实验发现变差项，建议复核', {}, { timeout: 5000 })
    assert.ok(screen.getByText(/通过率下降/))
    // 一句话结论:费用/Token 百分比变化 + 通过率 + 门禁
    assert.ok(screen.getByText(/费用变化 -40\.0%/))
    assert.ok(screen.getByText(/Token 变化 -30\.0%/))
    assert.ok(screen.getByText(/发布门禁 未计算/))
    // 数字卡有真实数字,涨跌着色(费用/Token 下降 = better;卡片与明细表各出现一次)
    assert.ok(screen.getAllByText('0.6').length >= 1)
    assert.ok(screen.getAllByText('700').length >= 1)
    const costCard = screen.getAllByText('0.6')[0].closest('div')
    assert.ok(costCard?.querySelector('.comparison-better'))
    // validate 在报告请求之前
    const validateAt = fetcher.calls.findIndex((url) => url.endsWith('/validate-pair'))
    const reportAt = fetcher.calls.findIndex((url) => url.endsWith('/compression-report'))
    assert.ok(validateAt >= 0 && reportAt > validateAt)
    assert.ok(screen.getByRole('button', { name: '下载报告' }))
  } finally {
    fetcher.restore()
  }
})

test('快照不一致的非法配对:无法对比卡 + 中文原因,不生成未知数字表', async () => {
  const fetcher = installFetch([
    { pattern: /suggest-pair/, body: PAIR_FOUND },
    {
      pattern: /validate-pair$/,
      status: 422,
      body: {
        error:
          '无法对比:用例快照不一致(hash-aaa 与 hash-bbb):总费用对比会被数据集差异污染,请用同一冻结版本重跑。',
        reasons: [
          {
            code: 'snapshot_mismatch',
            message:
              '用例快照不一致(hash-aaa 与 hash-bbb):总费用对比会被数据集差异污染,请用同一冻结版本重跑。',
          },
        ],
      },
    },
    { pattern: /comparisons$/, body: COMPARISON },
    { pattern: /compression-report$/, body: COMPRESSION },
    { pattern: /gates$/, body: { gates: [] } },
    { pattern: /runs$/, body: RUNS },
  ])
  try {
    render(<EvaluationApp />)
    fireEvent.click(screen.getByRole('button', { name: '成本与压缩' }))
    await screen.findByRole('alert')
    assert.ok(screen.getByText('无法对比'))
    assert.ok(screen.getByText(/用例快照不一致/))
    assert.ok(screen.getByText('(snapshot_mismatch)'))
    assert.ok(screen.getByRole('button', { name: '查看本次运行' }))
    // 非法配对不调用报告生成,也没有数字卡
    assert.ok(fetcher.calls.every((url) => !url.endsWith('/compression-report')))
    assert.equal(screen.queryByText('本次费用'), null)
  } finally {
    fetcher.restore()
  }
})

const RUN_DETAIL_COMPLETED = {
  runId: 'comp-run',
  manifest: {
    purpose: '压缩',
    mode: 'live',
    provider: 'cloud',
    dataset: {
      datasetId: 'ds-zh',
      version: 1,
      contentHash: 'hash-v1',
      frozenAt: '2026-09-01T00:00:00Z',
    },
    planCount: 2,
    grading: { judge: 'vlm' },
    legacy: false,
    parentRunId: null,
    recordingId: null,
    compression: { enabled: true },
  },
  status: 'completed',
  progress: {
    total: 2,
    pending: 0,
    running: 0,
    completed: 2,
    failed: 0,
    cancelled: 0,
    timedOut: 0,
  },
  extra: {},
  denominators: { plan: 2, attempted: 2, generated: 2, determined: 2, pass: 2 },
  metrics: { executionCoverage: 1, conditionalQualityPassRate: 1, planCompletionRate: 1 },
  annotations: [],
  coverageGaps: [],
  flow: null,
}

const TRIALS = {
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
      artifactIds: [],
      grades: [],
    },
  ],
}

test('运行详情一键配对:压缩跑找到基线,点击直达成本报告', async () => {
  const fetcher = installFetch([
    { pattern: /runs$/, body: RUNS },
    { pattern: /runs\/comp-run$/, body: RUN_DETAIL_COMPLETED },
    { pattern: /runs\/comp-run\/trials$/, body: TRIALS },
    { pattern: /runs\/comp-run\/cases$/, body: { cases: [] } },
    { pattern: /runs\/comp-run\/gates$/, body: { gates: [] } },
    { pattern: /suggest-pair\?run_id=comp-run$/, body: PAIR_FOUND },
    { pattern: /validate-pair$/, body: VALIDATED },
    { pattern: /comparisons$/, body: COMPARISON },
    { pattern: /compression-report$/, body: COMPRESSION },
    { pattern: /gates$/, body: { gates: [] } },
  ])
  try {
    render(<EvaluationApp />)
    fireEvent.click(screen.getByRole('button', { name: '运行详情' }))
    const pairButton = await screen.findByRole(
      'button',
      { name: '与未压缩基线 base-run 对比成本' },
      { timeout: 5000 },
    )
    fireEvent.click(pairButton)
    await screen.findByText('压缩实验发现变差项，建议复核', {}, { timeout: 5000 })
    assert.ok(screen.getByText(/费用变化 -40\.0%/))
  } finally {
    fetcher.restore()
  }
})

test('运行详情无配对:中文原因 + 一键新建配对运行预填压缩与 live', async () => {
  const fetcher = installFetch([
    {
      // 运行列表按时间倒序,首位即默认查看的运行;这里让未压缩基线排在最前。
      pattern: /runs$/,
      body: {
        runs: [
          {
            runId: 'base-run',
            createdAt: '2026-09-14T00:00:00Z',
            mode: 'live',
            provider: 'cloud',
            purpose: '基线',
            legacy: false,
            status: 'completed',
            planCount: 2,
            planHash: 'aa11',
            datasetId: 'ds-zh',
            compression: false,
          },
          {
            runId: 'comp-run',
            createdAt: '2026-09-12T00:00:00Z',
            mode: 'live',
            provider: 'cloud',
            purpose: '压缩',
            legacy: false,
            status: 'completed',
            planCount: 2,
            planHash: 'bb22',
            datasetId: 'ds-zh',
            compression: true,
          },
        ],
      },
    },
    {
      pattern: /runs\/base-run$/,
      body: {
        ...RUN_DETAIL_COMPLETED,
        runId: 'base-run',
        manifest: {
          ...RUN_DETAIL_COMPLETED.manifest,
          compression: { enabled: false },
          purpose: '基线',
        },
      },
    },
    { pattern: /runs\/base-run\/trials$/, body: TRIALS },
    { pattern: /runs\/base-run\/cases$/, body: { cases: [] } },
    { pattern: /runs\/base-run\/gates$/, body: { gates: [] } },
    {
      pattern: /suggest-pair\?run_id=base-run$/,
      body: {
        currentRunId: 'base-run',
        currentRole: 'baseline',
        pairRunId: null,
        candidates: [],
        reasons: [
          {
            code: 'no_counterpart',
            message: '没有找到与「base-run」同数据集、开启压缩的成功运行。',
          },
        ],
        guidance: {
          action: 'create_counterpart',
          message: '本运行未压缩,可作为基线;先跑一次同数据集、开启压缩的 live 运行,再回来对比。',
          prefill: {
            datasetId: 'ds-zh',
            version: 1,
            mode: 'live',
            compression: { enabled: true },
            purpose: '压缩对比运行',
            budget: { currency: 'CNY', maxCost: 5, prices: { generation: 0.5, judge: 0.05 } },
            judge: 'vlm',
            judgeConfig: { model: 'qwen-vl-max-latest' },
          },
        },
        coverage: null,
      },
    },
    { pattern: /datasets$/, body: DATASETS },
  ])
  try {
    render(<EvaluationApp />)
    fireEvent.click(screen.getByRole('button', { name: '运行详情' }))
    await screen.findByText('未找到可对比的压缩运行')
    assert.ok(screen.getByText(/没有找到与「base-run」同数据集、开启压缩的成功运行/))
    fireEvent.click(screen.getByRole('button', { name: '一键新建配对运行（同数据集、开启压缩）' }))
    // 跳到新建评测:数据集/压缩开关/live 模式/预算与裁判模型已预填
    await waitFor(() =>
      assert.equal((screen.getByLabelText(/live 模式/) as HTMLInputElement).checked, true),
    )
    assert.equal((screen.getByLabelText(/提示词压缩实验/) as HTMLInputElement).checked, true)
    assert.ok(screen.getByText(/已锁定数据集版本 v1/))
    await waitFor(() => {
      const judgeInput = screen.getByPlaceholderText('qwen-vl-max-latest') as HTMLInputElement
      assert.equal(judgeInput.value, 'qwen-vl-max-latest')
    })
  } finally {
    fetcher.restore()
  }
})
