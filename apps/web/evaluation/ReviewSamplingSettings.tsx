import type { ReviewSamplingConfig } from './api.js'
export const defaultReviewSampling: ReviewSamplingConfig = {
  enabled: true,
  required: 'fail_or_undetermined',
  riskRate: 1,
  randomRate: 0.05,
  riskThreshold: 0.8,
  riskMargin: 0.05,
  grayMin: 0.4,
  grayMax: 0.7,
  gateScoreThreshold: null,
  reviewAgentFlowFails: false,
  includeSynthetic: false,
}
export function ReviewSamplingSettings({
  value,
  onChange,
}: {
  value: ReviewSamplingConfig
  onChange: (value: ReviewSamplingConfig) => void
}) {
  const update = (patch: Partial<ReviewSamplingConfig>) => onChange({ ...value, ...patch })
  return (
    <fieldset className="eval-review-policy">
      <legend>自动人工审核</legend>
      <label>
        <input
          type="checkbox"
          checked={value.enabled}
          onChange={(event) => update({ enabled: event.target.checked })}
        />
        启用本次运行自动送审
      </label>
      <p className="review-policy-summary">
        {value.enabled
          ? `必送${value.required === 'none' ? '关闭' : '开启'} · 风险${value.riskRate > 0 ? '开启' : '关闭'} · 随机 ${(value.randomRate * 100).toFixed(0)}%`
          : '本次运行不自动创建审核任务'}
      </p>
      <details className="review-policy-details">
        <summary>调整送审规则与阈值</summary>
        <fieldset disabled={!value.enabled}>
          <label>
            <input
              type="checkbox"
              checked={value.required !== 'none'}
              onChange={(event) =>
                update({ required: event.target.checked ? 'fail_or_undetermined' : 'none' })
              }
            />
            必送：裁判异常、模糊带、门槛冲突或标记题
          </label>
          <label>
            <input
              type="checkbox"
              checked={value.riskRate > 0}
              onChange={(event) => update({ riskRate: event.target.checked ? 1 : 0 })}
            />
            风险：画质灰区、多裁判不一致
          </label>
          <label>
            <input
              type="checkbox"
              checked={value.randomRate > 0}
              onChange={(event) => update({ randomRate: event.target.checked ? 0.05 : 0 })}
            />
            随机抽检画质通过题
          </label>
          <label>
            抽检比例（%）
            <input
              type="number"
              min={0}
              max={100}
              value={value.randomRate * 100}
              onChange={(event) => update({ randomRate: Number(event.target.value) / 100 })}
            />
          </label>
          {(['grayMin', 'grayMax', 'riskThreshold', 'riskMargin'] as const).map((key) => (
            <label key={key}>
              {
                {
                  grayMin: '灰区下限',
                  grayMax: '灰区上限',
                  riskThreshold: '模糊带参考阈值',
                  riskMargin: '模糊带半宽 ε',
                }[key]
              }
              <input
                type="number"
                min={0}
                max={1}
                step={0.01}
                value={value[key]}
                onChange={(event) => update({ [key]: Number(event.target.value) })}
              />
            </label>
          ))}
          <label>
            质量分门槛（可选）
            <input
              type="number"
              min={0}
              max={1}
              step={0.01}
              value={value.gateScoreThreshold ?? ''}
              placeholder="未配置，不以分数推断门禁冲突"
              onChange={(event) =>
                update({
                  gateScoreThreshold: event.target.value === '' ? null : Number(event.target.value),
                })
              }
            />
          </label>
          <label>
            <input
              type="checkbox"
              checked={value.reviewAgentFlowFails}
              onChange={(event) => update({ reviewAgentFlowFails: event.target.checked })}
            />
            Agent 流程失败归因抽检（不改写规则分）
          </label>
          <label>
            <input
              type="checkbox"
              checked={value.includeSynthetic}
              onChange={(event) => update({ includeSynthetic: event.target.checked })}
            />
            包含 mock/stub 管线自测（默认排除）
          </label>
        </fieldset>
      </details>
      <p className="review-policy-note">Agent 规则与 mock/stub 效果自测默认不送审。</p>
      {value.grayMin > value.grayMax && <p role="alert">灰区下限不得大于上限。</p>}
    </fieldset>
  )
}
export function reviewReason(rule: string): string {
  const reasons: Record<string, string> = {
    judge_error: '裁判出错、超时或回答无法解析',
    judge_unreliable: '自动评分无法可靠判定',
    score_gate_conflict: '自动判定与质量分门槛冲突',
    score_ambiguous: '分数接近阈值，处于模糊带',
    flagged_case: '用例明确要求人工审核',
    score_gray: '画质得分处于灰区',
    judge_disagreement: '多个裁判的结果不一致',
    random: '画质通过题随机抽检',
    agent_flow_failure: 'Agent 流程失败归因抽检',
    manual: '手动送审',
    required_all: '显式要求全部效果题审核',
    judgeError: '裁判异常',
    riskSample: '风险抽检',
    randomSample: '随机抽检',
    agentFailureAnalysis: 'Agent 流程失败归因抽检',
    drift: '裁判漂移复核',
    'required=fail_or_undetermined': '历史策略：失败或无法判定',
    'required=all': '历史策略：全部审核',
  }
  return reasons[rule] ?? `未知送审原因：${rule}`
}
