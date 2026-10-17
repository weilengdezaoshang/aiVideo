import { EvaluationTable } from './EvaluationTable.js'
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ApiError,
  api,
  type ComparisonView,
  type CompressionReport,
  type GateView,
  type PairCoverage,
  type PairReason,
  type PairRunPrefill,
  type PairSuggestion,
  type RunListItem,
} from './api.js'

interface Report {
  comparison: ComparisonView
  compression: CompressionReport | null
  baseline: string
  candidate: string
  gates: GateView[]
  baselineGates: GateView[]
  coverage: PairCoverage | null
}

interface InvalidPair {
  baseline: string
  candidate: string
  reasons: PairReason[]
}

const number = (value: number | null, digits = 2) =>
  value === null ? '未知' : value.toLocaleString('zh-CN', { maximumFractionDigits: digits })
const rate = (value: number | null) => (value === null ? '未知' : `${(value * 100).toFixed(1)}%`)
const change = (value: number | null, suffix = '') =>
  value === null ? '未知' : `${value > 0 ? '+' : ''}${number(value, 4)}${suffix}`
const gateName = (value?: string) =>
  ({ pass: '通过', block: '阻断', fail: '失败', inconclusive: '证据不足' })[
    value?.toLowerCase() ?? ''
  ] ?? '未计算'
/** 相对基线的百分比变化;基线为 0 时退回绝对差值文案,不显示 Infinity。 */
const percentChange = (delta: number | null, base: number | null): string => {
  if (delta === null || base === null || base === 0) {
    return change(delta)
  }
  const pct = (delta / base) * 100
  return `${pct > 0 ? '+' : ''}${pct.toFixed(1)}%`
}

export function ComparisonWorkspace({
  initialRun = '',
  initialPair,
  auto = false,
  mode = 'cost',
  onOpenRun,
  onCreatePairRun,
}: {
  initialRun?: string
  initialPair?: { baseline: string; candidate: string } | null
  auto?: boolean
  mode?: 'cost' | 'compare'
  onOpenRun: (id: string) => void
  onCreatePairRun?: (prefill: PairRunPrefill) => void
}) {
  const [baseline, setBaseline] = useState(initialPair?.baseline ?? '')
  const [candidate, setCandidate] = useState(initialPair?.candidate ?? '')
  const [runs, setRuns] = useState<RunListItem[]>([])
  const [reason, setReason] = useState<string | null>(null)
  const [suggestion, setSuggestion] = useState<PairSuggestion | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [report, setReport] = useState<Report | null>(null)
  const [invalid, setInvalid] = useState<InvalidPair | null>(null)
  const operation = useRef(0)

  // 成本模式:先硬校验,通过才请求比较/压缩报告;不通过停在「无法对比」卡,不出未知数字表。
  const generate = useCallback(
    async (base: string, current: string) => {
      if (mode !== 'cost') {
        const id = ++operation.current
        setBusy(true)
        setError(null)
        setReport(null)
        try {
          const [comparison, gates, baselineGates] = await Promise.all([
            api.compare(base, current),
            api.gates(current),
            api.gates(base),
          ])
          if (id === operation.current) {
            setReport({
              comparison,
              compression: null,
              gates: gates.gates,
              baselineGates: baselineGates.gates,
              baseline: base,
              candidate: current,
              coverage: null,
            })
          }
        } catch (exc) {
          if (id === operation.current) {
            setError(exc instanceof Error ? exc.message : String(exc))
          }
        } finally {
          if (id === operation.current) {
            setBusy(false)
          }
        }
        return
      }
      const id = ++operation.current
      setBusy(true)
      setError(null)
      setReport(null)
      setInvalid(null)
      try {
        const validated = await api.validatePair(base, current)
        const [comparison, compression, gates, baselineGates] = await Promise.all([
          api.compare(base, current),
          api.compressionReport(base, current),
          api.gates(current),
          api.gates(base),
        ])
        if (id === operation.current) {
          setReport({
            comparison,
            compression,
            gates: gates.gates,
            baselineGates: baselineGates.gates,
            baseline: base,
            candidate: current,
            coverage: validated.coverage,
          })
        }
      } catch (exc) {
        if (id === operation.current) {
          if (exc instanceof ApiError && exc.reasons && exc.reasons.length > 0) {
            setInvalid({ baseline: base, candidate: current, reasons: exc.reasons })
          } else {
            setError(exc instanceof Error ? exc.message : String(exc))
          }
        }
      } finally {
        if (id === operation.current) {
          setBusy(false)
        }
      }
    },
    [mode],
  )

  useEffect(() => {
    let disposed = false
    setSuggestion(null)
    setInvalid(null)
    setReport(null)
    setError(null)
    if (initialPair) {
      setBaseline(initialPair.baseline)
      setCandidate(initialPair.candidate)
      setLoading(false)
      if (auto) {
        void generate(initialPair.baseline, initialPair.candidate)
      }
      return () => {
        disposed = true
        operation.current++
      }
    }
    const bootstrap =
      mode === 'cost'
        ? Promise.all([api.suggestPair(initialRun || undefined), api.runs()])
        : Promise.all([api.suggestBaseline(initialRun || undefined), api.runs()])
    void bootstrap
      .then(([first, list]) => {
        if (disposed) {
          return
        }
        setRuns(
          list.runs
            .filter((run) => run.status === 'completed')
            .sort((a, b) => b.createdAt.localeCompare(a.createdAt)),
        )
        if (mode === 'cost') {
          const pair = first as PairSuggestion
          setSuggestion(pair)
          if (pair.currentRunId && pair.pairRunId) {
            // 基线固定为未压缩一侧,本次固定为开启压缩一侧。
            const baselineId =
              pair.currentRole === 'compressed' ? pair.pairRunId : pair.currentRunId
            const candidateId =
              pair.currentRole === 'compressed' ? pair.currentRunId : pair.pairRunId
            setBaseline(baselineId)
            setCandidate(candidateId)
            if (auto) {
              void generate(baselineId, candidateId)
            }
          } else {
            setCandidate(pair.currentRunId ?? initialRun)
            setReason(
              pair.reasons.length
                ? pair.reasons.map((item) => item.message).join('；')
                : '暂无可对比的运行。',
            )
          }
        } else {
          const suggestion = first as {
            candidateRunId: string | null
            baselineRunId: string | null
            reason: string | null
          }
          setCandidate(suggestion.candidateRunId ?? initialRun)
          setBaseline(suggestion.baselineRunId ?? '')
          setReason(suggestion.reason)
          if (auto && suggestion.baselineRunId && suggestion.candidateRunId) {
            void generate(suggestion.baselineRunId, suggestion.candidateRunId)
          }
        }
      })
      .catch((exc: unknown) => {
        if (!disposed) {
          setError(exc instanceof Error ? exc.message : String(exc))
        }
      })
      .finally(() => {
        if (!disposed) {
          setLoading(false)
        }
      })
    return () => {
      disposed = true
      operation.current++
    }
  }, [initialRun, initialPair, auto, mode, generate])

  // 成本模式的手动更换是显式选择:选定本次运行后由用户在同数据集列表里挑基线,
  // 不做静默改写;系统推荐只在进入页面/运行详情入口时给出。
  const selectCandidate = async (id: string) => {
    const requestId = ++operation.current
    setCandidate(id)
    setReport(null)
    setInvalid(null)
    setReason(null)
    setError(null)
    setBusy(false)
    if (mode === 'cost') {
      setBaseline('')
      setSuggestion(null)
      setLoading(false)
      return
    }
    setLoading(true)
    try {
      const suggestion = await api.suggestBaseline(id)
      if (requestId === operation.current) {
        setBaseline(suggestion.baselineRunId ?? '')
        setReason(suggestion.reason)
      }
    } catch (exc) {
      if (requestId === operation.current) {
        setError(exc instanceof Error ? exc.message : String(exc))
      }
    } finally {
      if (requestId === operation.current) {
        setLoading(false)
      }
    }
  }

  const selectBaseline = (id: string) => {
    operation.current++
    setBusy(false)
    setBaseline(id)
    setReport(null)
    setInvalid(null)
  }

  const download = () => {
    if (!report) {
      return
    }
    const blob = new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `comparison-${report.candidate}.json`
    link.click()
    URL.revokeObjectURL(url)
  }

  // 高级手动选择:基线列表只显示与本次运行同数据集的成功运行。
  const candidateDataset = runs.find((run) => run.runId === candidate)?.datasetId
  const runOptions = (exclude: string, sameDatasetOnly: boolean) =>
    runs
      .filter((run) => run.runId !== exclude)
      .filter((run) => !sameDatasetOnly || !candidateDataset || run.datasetId === candidateDataset)
      .map((run) => (
        <option key={run.runId} value={run.runId}>
          {run.runId} · {run.purpose || run.createdAt}
          {run.compression === undefined ? '' : run.compression ? ' · 压缩开' : ' · 压缩关'}
        </option>
      ))

  const c = report?.compression
  const latestGate = [...(report?.gates ?? [])].sort((a, b) =>
    b.decidedAt.localeCompare(a.decidedAt),
  )[0]
  const mismatched =
    report && !['compatible', 'subject_comparison'].includes(report.comparison.comparability)
  const deltaQuality = mismatched ? null : (c?.quality.delta ?? null)
  const sampleIncomplete =
    report?.coverage &&
    report.coverage.alignedCompletedCases <
      Math.min(report.coverage.baselinePlan, report.coverage.compressedPlan)
  const worsening = report
    ? [
        c && c.cost.delta !== null && c.cost.delta > 0 ? `费用增加 ${change(c.cost.delta)}` : null,
        c && c.tokens.delta !== null && c.tokens.delta > 0
          ? `Token 增加 ${change(c.tokens.delta)}`
          : null,
        deltaQuality !== null && deltaQuality < 0
          ? `通过率下降 ${change(deltaQuality * 100, ' 个百分点')}`
          : null,
        ...report.comparison.checkRegressions.map(
          (item) => `${item.caseId} · ${item.checkId}：检查退化`,
        ),
        ...(latestGate?.verdict.toLowerCase() === 'block' ||
        latestGate?.verdict.toLowerCase() === 'fail'
          ? ['发布门禁未通过', ...latestGate.reasons]
          : []),
      ].filter((item): item is string => item !== null)
    : []
  return (
    <section className="comparison-workspace">
      <h2>{mode === 'cost' ? '成本与压缩' : '批次比较'}</h2>
      {error && (
        <p className="eval-error" role="alert">
          {error}
        </p>
      )}
      <div className="comparison-selection">
        <div>
          <span>{mode === 'cost' ? '基线(未压缩)' : '基线'}</span>
          <strong>{baseline || '尚无合适基线'}</strong>
        </div>
        <span className="comparison-arrow">→</span>
        <div>
          <span>{mode === 'cost' ? '本次(开启压缩)' : '本次'}</span>
          <strong>{candidate || '尚无成功运行'}</strong>
        </div>
        <button
          className="eval-primary"
          disabled={loading || busy || !baseline || !candidate || baseline === candidate}
          onClick={() => void generate(baseline, candidate)}
        >
          {busy ? '生成中…' : '生成对比报告'}
        </button>
      </div>
      {loading && <p>正在选择最近可对比运行…</p>}
      {mode === 'cost' && !loading && invalid && (
        <div className="comparison-invalid" role="alert">
          <h3>无法对比</h3>
          <p>
            {invalid.baseline} → {invalid.candidate}
            未通过配对校验,不生成报告;请修复后重试或换一组运行。
          </p>
          <ul>
            {invalid.reasons.map((item, index) => (
              <li key={`${item.code}:${index}`}>
                {item.message} <small>({item.code})</small>
              </li>
            ))}
          </ul>
          <div className="comparison-invalid-actions">
            <button type="button" onClick={() => onOpenRun(invalid.candidate)}>
              查看本次运行
            </button>
            {invalid.baseline && (
              <button type="button" onClick={() => onOpenRun(invalid.baseline)}>
                查看基线运行
              </button>
            )}
          </div>
        </div>
      )}
      {mode === 'cost' &&
        !loading &&
        !invalid &&
        !baseline &&
        suggestion &&
        !suggestion.pairRunId && (
          <div className="comparison-invalid pair-guidance">
            <h3>{suggestion.currentRunId ? '暂无可对比的配对' : '暂无可对比的运行'}</h3>
            {reason && <p>{reason}</p>}
            <ul>
              {suggestion.reasons.map((item, index) => (
                <li key={`${item.code}:${index}`}>
                  {item.message} <small>({item.code})</small>
                </li>
              ))}
            </ul>
            {suggestion.guidance && (
              <div className="pair-guidance-action">
                <p>{suggestion.guidance.message}</p>
                {onCreatePairRun && (
                  <button
                    type="button"
                    className="eval-primary"
                    onClick={() => onCreatePairRun(suggestion.guidance!.prefill)}
                  >
                    一键新建配对运行(同数据集
                    {`${suggestion.guidance.prefill.compression.enabled ? '、开启压缩' : '、关闭压缩'}`}
                    )
                  </button>
                )}
              </div>
            )}
            {!suggestion.guidance && suggestion.currentRunId && (
              <button type="button" onClick={() => onOpenRun(suggestion.currentRunId!)}>
                查看运行详情
              </button>
            )}
          </div>
        )}
      {mode === 'compare' && !loading && !baseline && (
        <div className="eval-empty">
          <h3>需要至少两次相关成功运行</h3>
          <p>{reason ?? '请选择成功运行作为基线。'}</p>
          <label>
            选择基线
            <select value={baseline} onChange={(e) => selectBaseline(e.target.value)}>
              <option value="">选择成功运行</option>
              {runOptions(candidate, false)}
            </select>
          </label>
        </div>
      )}
      <details className="comparison-options">
        <summary>更换对比对象</summary>
        <div>
          <label>
            本次运行
            <select value={candidate} onChange={(e) => void selectCandidate(e.target.value)}>
              <option value="">选择成功运行</option>
              {runOptions(baseline, false)}
            </select>
          </label>
          <label>
            基线运行
            <select value={baseline} onChange={(e) => selectBaseline(e.target.value)}>
              <option value="">选择成功运行{mode === 'cost' ? '(同数据集)' : ''}</option>
              {runOptions(candidate, mode === 'cost')}
            </select>
          </label>
        </div>
        <details>
          <summary>粘贴运行 ID</summary>
          <div>
            <label>
              基线 runId
              <input value={baseline} onChange={(e) => selectBaseline(e.target.value.trim())} />
            </label>
            <label>
              本次 runId
              <input
                value={candidate}
                onChange={(e) => {
                  operation.current++
                  setBusy(false)
                  setCandidate(e.target.value.trim())
                  setReport(null)
                  setInvalid(null)
                }}
              />
            </label>
          </div>
        </details>
      </details>
      {report && (
        <div className="comparison-report">
          <div className="comparison-conclusion">
            {mode === 'cost' && c ? (
              <>
                <h3>
                  {worsening.length
                    ? '压缩实验发现变差项，建议复核'
                    : sampleIncomplete
                      ? '对比成立，但样本不全'
                      : '压缩实验对比成立'}
                  {sampleIncomplete && (
                    <small className="sample-incomplete">
                      样本不全：对齐 {report.coverage?.alignedCompletedCases} / 计划{' '}
                      {Math.min(
                        report.coverage?.baselinePlan ?? 0,
                        report.coverage?.compressedPlan ?? 0,
                      )}{' '}
                      个用例
                    </small>
                  )}
                </h3>
                <p>
                  费用变化 {percentChange(c.cost.delta, c.cost.baselineSettled)}；Token 变化{' '}
                  {percentChange(c.tokens.delta, c.tokens.baseline.providerReportedTokens)}
                  ；通过率{' '}
                  {deltaQuality === null || deltaQuality === 0
                    ? '持平'
                    : `${change(deltaQuality * 100, ' 个百分点')}`}
                  ；发布门禁 {gateName(latestGate?.verdict)}。
                </p>
                <p className="comparison-promote">{c.promoteReason}</p>
              </>
            ) : (
              <>
                <h3>
                  {mismatched
                    ? '配置或快照有差异，仅供交集比较'
                    : worsening.length
                      ? '发现变差项，建议复核'
                      : '未发现已知指标退化'}
                </h3>
                <p>
                  通过率 {rate(report.comparison.summary.baselinePassRate)} →{' '}
                  {rate(report.comparison.summary.candidatePassRate)}；发布门禁{' '}
                  {gateName(latestGate?.verdict)}。
                </p>
                {mismatched && <p>{report.comparison.note}</p>}
              </>
            )}
          </div>
          {mode === 'cost' && c && (
            <div className="comparison-numbers">
              {(
                [
                  [
                    '本次费用',
                    number(c.cost.compressedSettled, 4),
                    c.cost.delta,
                    false,
                    c.cost.compressedSettled,
                  ],
                  [
                    '本次 Token',
                    number(c.tokens.compressed.providerReportedTokens, 0),
                    c.tokens.delta,
                    false,
                    c.tokens.compressed.providerReportedTokens,
                  ],
                  [
                    '本次通过率',
                    rate(c.quality.compressedPassRate),
                    deltaQuality === null ? null : deltaQuality * 100,
                    true,
                    c.quality.compressedPassRate,
                  ],
                ] as [string, string, number | null, boolean, number | null][]
              ).map(([label, value, delta, higher, raw]) => (
                <div key={label}>
                  <span>{label}</span>
                  <strong>{value}</strong>
                  {raw === null && (
                    <em className="comparison-unknown">本 run 未记账，不计入成功对比</em>
                  )}
                  {raw !== null && (
                    <em
                      className={
                        typeof delta !== 'number' || delta === 0
                          ? ''
                          : (higher ? delta < 0 : delta > 0)
                            ? 'comparison-worse'
                            : 'comparison-better'
                      }
                    >
                      {change(typeof delta === 'number' ? delta : null, higher ? ' 个百分点' : '')}
                    </em>
                  )}
                </div>
              ))}
              <div>
                <span>发布门禁</span>
                <strong>{gateName(latestGate?.verdict)}</strong>
                <em>
                  {c.compressionEnabled
                    ? `压缩质量门槛：${c.promote === null ? '未知' : c.promote ? '满足' : '不满足'}`
                    : '本次未启用压缩'}
                </em>
              </div>
            </div>
          )}
          <EvaluationTable className="eval-table">
            <thead>
              <tr>
                <th>指标</th>
                <th>基线</th>
                <th>本次</th>
                <th>变化</th>
              </tr>
            </thead>
            <tbody>
              {mode === 'cost' &&
                c &&
                (
                  [
                    [
                      '已结算费用',
                      number(c.cost.baselineSettled, 4),
                      number(c.cost.compressedSettled, 4),
                      c.cost.delta,
                      false,
                    ],
                    [
                      '服务商 Token',
                      number(c.tokens.baseline.providerReportedTokens, 0),
                      number(c.tokens.compressed.providerReportedTokens, 0),
                      c.tokens.delta,
                      false,
                    ],
                    [
                      '通过率',
                      rate(c.quality.baselinePassRate),
                      rate(c.quality.compressedPassRate),
                      deltaQuality === null ? null : deltaQuality * 100,
                      true,
                    ],
                  ] as [string, string, string, number | null, boolean][]
                ).map(([label, base, current, delta, higher]) => (
                  <tr key={label}>
                    <th>{label}</th>
                    <td>{base}</td>
                    <td>{current}</td>
                    <td
                      className={
                        typeof delta !== 'number' || delta === 0
                          ? ''
                          : (higher ? delta < 0 : delta > 0)
                            ? 'comparison-worse'
                            : 'comparison-better'
                      }
                    >
                      {change(typeof delta === 'number' ? delta : null, higher ? ' 个百分点' : '')}
                    </td>
                  </tr>
                ))}
              <tr>
                <th>发布门禁</th>
                <td>
                  {gateName(
                    [...(report.baselineGates ?? [])].sort((a, b) =>
                      b.decidedAt.localeCompare(a.decidedAt),
                    )[0]?.verdict,
                  )}
                </td>
                <td>{gateName(latestGate?.verdict)}</td>
                <td>{latestGate ? latestGate.reasons.join('；') || '无阻断原因' : '未计算'}</td>
              </tr>
            </tbody>
          </EvaluationTable>
          <h3>异常与退化</h3>
          {worsening.length ? (
            <ul className="comparison-worse">
              {worsening.map((item, i) => (
                <li key={i}>{item}</li>
              ))}
            </ul>
          ) : (
            <p>未发现已知变差项。</p>
          )}
          <details className="comparison-case-details">
            <summary>用例配对与错误明细</summary>
            <EvaluationTable className="eval-table">
              <thead>
                <tr>
                  <th>用例</th>
                  <th>基线分</th>
                  <th>本次分</th>
                  <th>变化</th>
                </tr>
              </thead>
              <tbody>
                {report.comparison.paired.map((pair) => (
                  <tr key={pair.caseId}>
                    <td>{pair.caseId}</td>
                    <td>{number(pair.baseline.score)}</td>
                    <td>{number(pair.candidate.score)}</td>
                    <td>{change(pair.delta)}</td>
                  </tr>
                ))}
              </tbody>
            </EvaluationTable>
            {report.comparison.checkRegressions.map((item) => (
              <p key={`${item.caseId}:${item.checkId}`}>
                {item.caseId} · {item.checkId} · {item.candidateGradeId}
              </p>
            ))}
            {report.comparison.candidateErrorCategories.map((item) => (
              <p key={item.category}>
                {item.category}：{item.trialIds.join('、')}
              </p>
            ))}
          </details>
          <details>
            <summary>覆盖与统计口径</summary>
            <p>
              配对 {report.comparison.coverage.pairedCount}；独立样本：
              {report.comparison.independentSamples ? '是' : '否'}。
              {mode === 'cost' && report.coverage
                ? ` 对齐用例 ${report.coverage.alignedCases}(两侧均完成 ${report.coverage.alignedCompletedCases});基线计划 ${report.coverage.baselinePlan}、本次计划 ${report.coverage.compressedPlan}。`
                : ''}
              {report.comparison.summary.uncertainty}
            </p>
            {c && (
              <p>
                {c.cost.note}；{c.tokens.note}；{c.promoteReason}
              </p>
            )}
            {mode === 'compare' && <p>{report.comparison.configDifferences.join('；')}</p>}
          </details>
          <div className="comparison-report-actions">
            <button onClick={download}>下载报告</button>
            <button onClick={() => onOpenRun(report.baseline)}>查看基线运行</button>
            <button onClick={() => onOpenRun(report.candidate)}>查看本次运行</button>
          </div>
        </div>
      )}
    </section>
  )
}
