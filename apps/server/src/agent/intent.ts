import { readFileSync } from 'node:fs'
import type { LlmProvider } from '../ports/index.js'
import { z } from 'zod'

const lexiconSchema = z.object({
  video: z.array(z.string()),
  image_only: z.array(z.string()),
  ignore_ref: z.array(z.string()),
  ambiguous: z.array(z.string()),
  direct_i2v: z.array(z.string()),
})
// JSON is a YAML 1.2 subset; the checked-in lexicon needs no runtime YAML dependency.
const lexicon = lexiconSchema.parse(
  JSON.parse(
    readFileSync(new URL('../../../../config/intent_lexicon.yaml', import.meta.url), 'utf8'),
  ),
)
const words = (kind: keyof typeof lexicon, prompt: string) =>
  lexicon[kind].some((pattern) => new RegExp(pattern, 'i').test(prompt))

export const intentResultSchema = z.object({
  version: z.literal('1').default('1'),
  goal: z.enum(['image', 'video', 'region_edit', 'analyze', 'unknown']),
  path: z.enum([
    'text_to_image',
    'image_edit',
    'text_to_video',
    'image_to_multiframe',
    'image_to_video',
    'clarify',
  ]),
  needSubjectCutout: z.boolean().default(false),
  frameCount: z.number().int().min(1).max(8).optional(),
  durationSec: z.number().positive().max(120).optional(),
  referenceAssetIds: z.array(z.string()).default([]),
  confidence: z.number().min(0).max(1).default(1),
  clarifyQuestion: z.string().optional(),
  clarifySlots: z.array(z.string()).optional(),
  hasImage: z.boolean().default(false),
  explicitDirectI2v: z.boolean().default(false),
  source: z.enum(['rules', 'llm']).default('llm'),
  needsClarification: z.boolean().default(false),
  clarification: z.string().optional(),
  /** Pending path is a proposal only; no generation until the user confirms. */
  proposedPath: z
    .enum(['text_to_image', 'text_to_video', 'image_to_multiframe', 'image_to_video'])
    .optional(),
})
export type IntentResult = z.infer<typeof intentResultSchema>
export interface CanvasContext {
  projectId: string
  nodeId?: string
  nodeType?: string
  assetUrls?: string[]
  assetVersions?: string[]
  hasImage: boolean
  selectedGroupIds?: string[]
  width?: number
  height?: number
}
export interface IntentInput {
  prompt: string
  imageRefs?: string[]
  canvas?: CanvasContext
  quota?: { creditsLeft?: number; rateLimited?: boolean }
  requestedGoal?: 'image' | 'video'
}
export interface TurnInput {
  userText: string
  canvas: CanvasContext
  flags: { videoToolEnabled: boolean; multiframeEnabled: boolean; directI2vEnabled: boolean }
  quota: NonNullable<IntentInput['quota']>
}
export interface ClarificationReply {
  imageRefs?: string[]
  goal?: 'image' | 'video'
}
export interface RoutingOptions {
  maxParallelSteps?: number
  intentMode?: 'rules_only' | 'rules_plus_llm'
  planValidator?: 'enforce'
  imageVideoStrategy?: 'multiframe_first' | 'allow_direct_i2v'
  videoToolEnabled?: boolean
  multiframeEnabled?: boolean
  directI2vEnabled?: boolean
  defaultFrameCount?: number
  maxFrameCount?: number
  referenceUrlAllowedHosts?: readonly string[]
  intentLlm?: IntentLLM
}
export const intentJsonSchema = z.toJSONSchema(intentResultSchema)
export interface IntentLLM {
  classify(input: IntentInput, schema: typeof intentJsonSchema): Promise<unknown>
}
/** Existing LLM port receives a constrained JSON tool, never free-text intent. */
export class SchemaIntentLLM implements IntentLLM {
  constructor(private provider: LlmProvider) {}
  async classify(input: IntentInput, schema: typeof intentJsonSchema): Promise<unknown> {
    const result = await this.provider.act({
      agent: 'host',
      temperature: 0,
      messages: [
        {
          role: 'system',
          content:
            '识别最终交付目标，调用 intent_result 返回 JSON。低温度分类。含图视频默认 image_to_multiframe；无图视频 text_to_video。禁止以静图替代视频。confidence<0.6 或含糊需求必须 clarify，并提出可确认的完整路径。',
        },
        { role: 'user', content: JSON.stringify(input) },
      ],
      tools: [{ name: 'intent_result', parameters: schema }],
    })
    return result.toolCalls?.find((call) => call.name === 'intent_result')?.args
  }
}
export class RoutingError extends Error {
  constructor(
    public code:
      | 'video_unavailable'
      | 'intent_invalid'
      | 'plan_invalid'
      | 'reference_unavailable'
      | 'quota_exceeded'
      | 'capability_unavailable'
      | 'capability_unbound'
      | 'spec_not_allowed'
      | 'prod_mock',
    message: string,
  ) {
    super(`${code}: ${message}`)
  }
}
export function ignoresReference(prompt: string) {
  return words('ignore_ref', prompt)
}
export function explicitDirect(prompt: string) {
  return words('direct_i2v', prompt)
}
export function referenceAssets(input: IntentInput): string[] {
  return input.imageRefs ?? input.canvas?.assetUrls ?? []
}
/** Validate before any LLM/provider sees remote references. Opaque asset IDs are not fetch URLs. */
export function validateReferenceUrls(refs: string[], options: RoutingOptions) {
  const allowed = options.referenceUrlAllowedHosts ?? ['localhost', '127.0.0.1']
  for (const ref of refs) {
    if (/^asset:[a-zA-Z0-9_-]+$/.test(ref)) {
      continue
    }
    let url: URL
    try {
      url = new URL(ref)
    } catch {
      throw new RoutingError('reference_unavailable', '参考图必须使用已登记资产或白名单 URL')
    }
    if (
      !['https:', 'http:'].includes(url.protocol) ||
      url.username ||
      url.password ||
      !allowed.includes(url.hostname) ||
      (url.protocol === 'http:' && !['localhost', '127.0.0.1'].includes(url.hostname))
    ) {
      throw new RoutingError('reference_unavailable', '参考 URL 不在白名单，请更换参考图')
    }
  }
}
export function clarifyIntent(
  intent: IntentResult,
  question: string,
  slots: string[] = [],
): IntentResult {
  return {
    ...intent,
    proposedPath:
      intent.path === 'clarify' || intent.path === 'image_edit' ? intent.proposedPath : intent.path,
    path: 'clarify',
    needsClarification: true,
    clarification: question,
    clarifyQuestion: question,
    clarifySlots: slots,
  }
}
export class IntentValidator {
  validate(value: unknown, input: IntentInput, options: RoutingOptions = {}): IntentResult {
    const parsed = intentResultSchema.safeParse(value)
    if (!parsed.success) {
      throw new RoutingError('intent_invalid', '意图 JSON 不符合 schema')
    }
    let intent = parsed.data
    const refs = referenceAssets(input)
    validateReferenceUrls(refs, options)
    if (
      input.quota?.rateLimited ||
      (input.quota?.creditsLeft !== undefined && input.quota.creditsLeft <= 0)
    ) {
      throw new RoutingError('quota_exceeded', '当前额度不足或请求已限流')
    }
    const hasImage = refs.length > 0 || input.canvas?.hasImage === true
    if (value && typeof value === 'object' && 'hasImage' in value && intent.hasImage !== hasImage) {
      throw new RoutingError('intent_invalid', '图片上下文不一致')
    }
    if (
      intent.referenceAssetIds.length &&
      intent.referenceAssetIds.some((id) => !refs.includes(id))
    ) {
      throw new RoutingError('intent_invalid', '参考资产不在当前上下文')
    }
    intent.referenceAssetIds = ignoresReference(input.prompt) ? [] : refs
    const proposed = intent.path === 'clarify' ? intent.proposedPath : intent.path
    if (intent.goal === 'image' && proposed && proposed !== 'text_to_image') {
      throw new RoutingError('intent_invalid', 'P0 不支持图片编辑路径')
    }
    if (
      value &&
      typeof value === 'object' &&
      'explicitDirectI2v' in value &&
      intent.explicitDirectI2v !== explicitDirect(input.prompt)
    ) {
      throw new RoutingError('intent_invalid', '直接图生视频必须由用户明确提出')
    }
    intent.explicitDirectI2v = explicitDirect(input.prompt)
    if (intent.goal === 'video') {
      const useImage = hasImage && !ignoresReference(input.prompt)
      const direct =
        useImage &&
        explicitDirect(input.prompt) &&
        options.directI2vEnabled === true &&
        options.imageVideoStrategy === 'allow_direct_i2v'
      const expected = !useImage
        ? 'text_to_video'
        : direct
          ? 'image_to_video'
          : 'image_to_multiframe'
      if (proposed && proposed !== expected) {
        throw new RoutingError('intent_invalid', '视频路径不符合图片上下文与直接图生视频开关')
      }
      if (expected === 'image_to_multiframe') {
        intent.frameCount ??= options.defaultFrameCount ?? 4
        if (intent.frameCount < 2 || intent.frameCount > (options.maxFrameCount ?? 8)) {
          throw new RoutingError('intent_invalid', '多帧数量须在 2 与 MAX_FRAME_COUNT 之间')
        }
      }
      if (useImage && !refs.length) {
        intent = clarifyIntent(
          { ...intent, proposedPath: expected, path: 'clarify' },
          '请选择一张可用参考图',
          ['reference_image'],
        )
      }
    }
    if (['unknown', 'analyze', 'region_edit'].includes(intent.goal)) {
      intent = clarifyIntent(
        { ...intent, proposedPath: 'text_to_image', path: 'clarify' },
        '当前 P0 支持图片或视频创作，是否按图片创作？',
        ['goal'],
      )
    }
    if (intent.goal === 'video' && /局部|区域修改|部分修改/.test(input.prompt)) {
      intent = clarifyIntent(intent, '是否先制作完整视频？局部修改尚未启用。', ['edit_scope'])
    }
    if (
      (refs.length > 1 || (input.canvas?.selectedGroupIds?.length ?? 0) > 1) &&
      !ignoresReference(input.prompt)
    ) {
      intent = clarifyIntent(
        intent,
        '检测到多张参考图。确认后以第一张为主参考继续，或回复选择另一张。',
        ['reference_image'],
      )
    }
    if (intent.confidence < 0.6 || intent.needsClarification || intent.path === 'clarify') {
      intent = clarifyIntent(
        intent,
        intent.clarifyQuestion ?? intent.clarification ?? '需求还不明确，请确认创作目标与路径',
        intent.clarifySlots,
      )
    }
    return intent
  }
}
export class IntentGate {
  constructor(private options: RoutingOptions = {}) {}
  async resolve(input: IntentInput): Promise<IntentResult> {
    const refs = referenceAssets(input)
    validateReferenceUrls(refs, this.options)
    if (
      input.quota?.rateLimited ||
      (input.quota?.creditsLeft !== undefined && input.quota.creditsLeft <= 0)
    ) {
      throw new RoutingError('quota_exceeded', '当前额度不足或请求已限流')
    }
    const hasImage = refs.length > 0 || input.canvas?.hasImage === true
    const positivePrompt = input.prompt.replace(
      /(?:不要|不需要|无需|不做)\s*(?:视频|动画|短片)/g,
      '',
    )
    const goal =
      input.requestedGoal ??
      (words('video', positivePrompt) && !words('image_only', input.prompt) ? 'video' : 'image')
    const explicitDirectI2v = explicitDirect(input.prompt)
    const direct =
      explicitDirectI2v &&
      this.options.directI2vEnabled &&
      this.options.imageVideoStrategy === 'allow_direct_i2v'
    const useImage = hasImage && !ignoresReference(input.prompt)
    let value: unknown = {
      version: '1',
      goal,
      hasImage,
      explicitDirectI2v,
      source: 'rules',
      needsClarification: false,
      needSubjectCutout: /主体|抠图/.test(input.prompt) && useImage,
      referenceAssetIds: refs,
      confidence: 1,
      path:
        goal === 'image'
          ? 'text_to_image'
          : !useImage
            ? 'text_to_video'
            : direct
              ? 'image_to_video'
              : 'image_to_multiframe',
    }
    const ambiguous =
      words('ambiguous', input.prompt) &&
      !/视频|动画|短片|关键帧|海报|video|animation/i.test(input.prompt)
    if (ambiguous) {
      if (this.options.intentMode === 'rules_plus_llm') {
        if (!this.options.intentLlm) {
          throw new RoutingError('intent_invalid', 'rules_plus_llm 未配置 IntentLLM')
        }
        value = await this.options.intentLlm.classify(input, intentJsonSchema)
      } else {
        const draft = intentResultSchema.parse(value)
        value = clarifyIntent(draft, '需求还不明确，是否先按图片创作？')
      }
    }
    return new IntentValidator().validate(value, input, this.options)
  }
}
export function routingOptionsFromEnv(env: NodeJS.ProcessEnv = process.env): RoutingOptions {
  const intentMode = env.INTENT_MODE ?? 'rules_only'
  const planValidator = env.PLAN_VALIDATOR ?? 'enforce'
  const imageVideoStrategy = env.IMAGE_VIDEO_STRATEGY ?? 'multiframe_first'
  if (
    !['rules_only', 'rules_plus_llm'].includes(intentMode) ||
    planValidator !== 'enforce' ||
    !['multiframe_first', 'allow_direct_i2v'].includes(imageVideoStrategy)
  ) {
    throw new Error('意图/计划门禁配置不合法')
  }
  const flag = (name: string) => {
    if (env[name] !== undefined && !['true', 'false'].includes(env[name]!)) {
      throw new Error(`${name} 必须为 true 或 false`)
    }
    return env[name] === 'true'
  }
  const count = (name: string, fallback: number) => {
    const value = Number(env[name] ?? fallback)
    if (!Number.isInteger(value) || value < 2 || value > 8) {
      throw new Error(`${name} 必须为 2–8 整数`)
    }
    return value
  }
  const defaultFrameCount = count('DEFAULT_FRAME_COUNT', 4)
  const maxFrameCount = count('MAX_FRAME_COUNT', 8)
  if (defaultFrameCount > maxFrameCount) {
    throw new Error('DEFAULT_FRAME_COUNT 超过 MAX_FRAME_COUNT')
  }
  return {
    intentMode: intentMode as RoutingOptions['intentMode'],
    planValidator: 'enforce',
    imageVideoStrategy: imageVideoStrategy as RoutingOptions['imageVideoStrategy'],
    videoToolEnabled: flag('VIDEO_TOOL_ENABLED'),
    multiframeEnabled: flag('MULTIFRAME_ENABLED'),
    directI2vEnabled: flag('DIRECT_I2V_ENABLED'),
    defaultFrameCount,
    maxFrameCount,
    maxParallelSteps: (() => {
      const value = Number(env.MAX_PARALLEL_STEPS ?? 4)
      if (!Number.isSafeInteger(value) || value < 1 || value > 32) {
        throw new Error('MAX_PARALLEL_STEPS 必须为 1–32 整数')
      }
      return value
    })(),
    referenceUrlAllowedHosts: env.REFERENCE_URL_ALLOWED_HOSTS?.split(',')
      .map((host) => host.trim())
      .filter(Boolean),
  }
}
