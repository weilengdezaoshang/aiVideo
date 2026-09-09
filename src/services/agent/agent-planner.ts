import { logger } from '../../logger.js'

/**
 * Agent 动作规划器(PRD §7 用例 A):
 *   显式引用一张图 → 计划(识别主体 → 用户检查蒙版 → 抠图并放到原图右侧)。
 *
 * 结构约束(不可妥协):
 *   - 计划必须受 schema 限制:步骤 key 只能来自工具白名单 TOOL_KEYS;
 *   - 服务端验证计划,越权/未知步骤直接拒绝,不让前端裸信模型输出;
 *   - 无显式引用时必须澄清(need_reference),零工具调用、不执行;
 *   - 混合超范围请求(如"抠图再生成视频")先澄清,不擅自裁剪用户意图。
 *
 * 规划通道:配置了云端文本模型(cloudTextModel + apiKey)时用模型输出,
 * 校验失败或未配置时降级为确定性规则规划。规则规划只是降级路径,
 * 不能替代模型规划参与 A11 验收(PRD §7:不能把关键词分支验收成 LLM Agent)。
 */

export type AgentToolKey = 'detect_mask' | 'wait_user_check_mask' | 'apply_cutout_place_right'
export const TOOL_KEYS: readonly AgentToolKey[] = [
  'detect_mask',
  'wait_user_check_mask',
  'apply_cutout_place_right',
]

const TOOL_LABELS: Record<AgentToolKey, string> = {
  detect_mask: '识别图片主体,准备蒙版',
  wait_user_check_mask: '请你检查蒙版',
  apply_cutout_place_right: '生成透明图片并放到原图右侧',
}

/** 首版只开放用例 A;这些关键词出现在请求里即视为超出当前能力范围 */
const OUT_OF_SCOPE_RE = /(视频|配音|分镜|拼接|时间线|网页|搜索|批量|多张图|全部图片|上色|扩图|超清)/
/** 用例 A 的意图关键词 */
const CUTOUT_INTENT_RE = /(抠|主体|透明|背景|剪影|分离|去背)/

export interface AgentPlan {
  summary: string
  steps: Array<{ key: AgentToolKey; label: string }>
  /** 计划说明卡片展示:源图 1 张、输出 1 张、需人工确认 */
  meta: { sourceImages: number; outputImages: number; needManualConfirm: boolean }
}

export type PlanResult =
  | { ok: true; plan: AgentPlan; plannedBy: 'model' | 'rules' }
  | { ok: false; reason: 'need_reference' | 'out_of_scope' | 'unclear'; message: string }

/** 组装用例 A 的固定计划(键序 = 执行序)。 */
export function buildUseCaseAPlan(): AgentPlan {
  return {
    summary: '识别主体 → 请你检查蒙版 → 生成透明图片并放到右侧',
    steps: TOOL_KEYS.map((key) => ({ key, label: TOOL_LABELS[key] })),
    meta: { sourceImages: 1, outputImages: 1, needManualConfirm: true },
  }
}

/** 确定性规则规划(降级通道):只接受明确的用例 A 请求,其余一律澄清。 */
export function planWithRules(request: string): PlanResult {
  const text = request.trim()
  if (!text) {
    return { ok: false, reason: 'unclear', message: '请描述你想让助手做什么' }
  }
  if (OUT_OF_SCOPE_RE.test(text)) {
    return {
      ok: false,
      reason: 'out_of_scope',
      message: '这个请求超出了当前助手能力(目前只支持:把一张图的主体抠出并放到原图旁)。请拆开或换个说法。',
    }
  }
  if (!CUTOUT_INTENT_RE.test(text)) {
    return {
      ok: false,
      reason: 'unclear',
      message:
        '还不确定你的意图。当前助手支持:把引用图片的主体抠出来放到旁边。可以试试"把这张图的主体抠出来,放到旁边"。',
    }
  }
  return { ok: true, plan: buildUseCaseAPlan(), plannedBy: 'rules' }
}

export interface ModelPlanOptions {
  apiKey: string
  baseUrl: string
  model: string
  request: string
  signal: AbortSignal
  fetchImpl?: typeof fetch
}

/**
 * 模型规划:让文本模型在受 schema 限制的 JSON 里输出计划;
 * 输出必须逐项通过白名单校验,任何越权/畸形都视为无效并降级规则规划。
 */
export async function planWithModel(options: ModelPlanOptions): Promise<PlanResult> {
  const { apiKey, baseUrl, model, request, signal, fetchImpl = (...args) => fetch(...args) } = options
  const system = [
    '你是画布创作助手的能力规划器。当前只开放一个用例:',
    '把用户显式引用的一张图片的主体抠出(识别主体蒙版 → 用户人工检查蒙版 → 生成透明 PNG 并放到原图右侧)。',
    '工具白名单(key 必须逐字一致):detect_mask, wait_user_check_mask, apply_cutout_place_right。',
    '只输出 JSON,不要 markdown 代码块,格式:',
    '{"capable": true|false, "reason": "need_reference|out_of_scope|unclear", "message": "给用户看的中文说明",',
    ' "plan": {"summary": "中文一句话", "steps": [{"key": "...", "label": "中文步骤名"}],',
    ' "meta": {"sourceImages": 1, "outputImages": 1, "needManualConfirm": true}}}',
    '规则:请求与用例无关或混合了其他能力 → capable=false 并给 reason;',
    '步骤只能是白名单键的有序子集;用户没有显式引用图片时 reason=need_reference。',
  ].join('\n')
  const res = await fetchImpl(`${baseUrl}/chat/completions`, {
    method: 'POST',
    signal,
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${apiKey}` },
    body: JSON.stringify({
      model,
      temperature: 0,
      max_tokens: 600,
      messages: [
        { role: 'system', content: system },
        { role: 'user', content: request },
      ],
    }),
  })
  const body = (await res.json().catch(() => ({}))) as {
    choices?: Array<{ message?: { content?: string } }>
  }
  const text = body?.choices?.[0]?.message?.content?.trim() ?? ''
  if (!text) {
    throw new Error('模型规划输出为空')
  }
  // 容错:剥掉偶尔出现的 ```json 围栏
  const jsonText = text.replace(/^```(?:json)?\s*/i, '').replace(/```\s*$/, '')
  const parsed = JSON.parse(jsonText) as {
    capable?: boolean
    reason?: string
    message?: string
    plan?: {
      summary?: string
      steps?: Array<{ key?: string; label?: string }>
      meta?: Record<string, unknown>
    }
  }
  if (!parsed.capable) {
    const reason = ['need_reference', 'out_of_scope', 'unclear'].includes(String(parsed.reason))
      ? (parsed.reason as 'need_reference' | 'out_of_scope' | 'unclear')
      : 'unclear'
    return { ok: false, reason, message: parsed.message || '这个请求需要先澄清,助手没有执行任何操作。' }
  }
  const steps = (parsed.plan?.steps ?? []).filter(
    (s): s is { key: AgentToolKey; label: string } =>
      typeof s?.key === 'string' &&
      typeof s?.label === 'string' &&
      (TOOL_KEYS as readonly string[]).includes(s.key),
  )
  if (steps.length === 0) {
    throw new Error('模型计划没有任何合法步骤')
  }
  // 执行序必须与白名单定义的能力依赖一致:检测 → 等用户 → 应用
  const order = Object.fromEntries(TOOL_KEYS.map((k, i) => [k, i]))
  steps.sort((a, b) => order[a.key] - order[b.key])
  return {
    ok: true,
    plannedBy: 'model',
    plan: {
      summary: parsed.plan?.summary?.slice(0, 200) || steps.map((s) => s.label).join(' → '),
      steps,
      meta: { sourceImages: 1, outputImages: 1, needManualConfirm: true },
    },
  }
}

/** 规划入口:模型优先,任何失败降级规则;两通道都产出受白名单约束的计划。 */
export async function planAgentAction(
  request: string,
  options: Partial<ModelPlanOptions> & { logger?: typeof logger } = {},
): Promise<PlanResult> {
  const { apiKey, baseUrl, model, signal, fetchImpl } = options
  if (apiKey && baseUrl && model) {
    try {
      return await planWithModel({
        apiKey,
        baseUrl,
        model,
        request,
        signal: signal ?? AbortSignal.timeout(15_000),
        fetchImpl,
      })
    } catch (err) {
      logger.warn('模型规划失败,降级规则规划', { err })
    }
  }
  return planWithRules(request)
}
