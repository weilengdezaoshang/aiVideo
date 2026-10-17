import { executionDag, validateDag, toolCapabilities } from './scheduler.js'
import { randomUUID } from 'node:crypto'
import { z } from 'zod'
import type { ToolCall } from '../domain/model.js'
import type { IntentResult, RoutingOptions } from './intent.js'
import { RoutingError } from './intent.js'
export const planValidationSchema = z.object({
  valid: z.boolean(),
  code: z
    .enum([
      'video_unavailable',
      'plan_invalid',
      'capability_unavailable',
      'capability_unbound',
      'spec_not_allowed',
      'prod_mock',
    ])
    .optional(),
  errors: z.array(z.string()),
})
export type PlanValidation = z.infer<typeof planValidationSchema>
const imageNames = ['image_generate', 'creative_agent_mcp_gen_text2image_v3']
/** Planner fills prompts; tool sequence is owned by the path template. */
export function videoTemplate(
  intent: IntentResult,
  prompt: string,
  imageRefs: string[],
  framePrompts?: string[],
): ToolCall[] {
  const call = (name: string, text: string, extra: object = {}): ToolCall => ({
    id: randomUUID(),
    name,
    args: { prompt: text, imageRefs, ...extra },
  })
  if (intent.path === 'text_to_video') {
    return [call('video_generate', prompt)]
  }
  if (intent.path === 'image_to_video') {
    return [call('image_to_video', prompt)]
  }
  const frames =
    framePrompts && framePrompts.length >= 2
      ? framePrompts
      : Array.from(
          { length: intent.frameCount ?? 4 },
          (_, index) => `${prompt}；关键帧 ${index + 1}`,
        )
  const cutout = intent.needSubjectCutout ? call('subject_cutout', prompt) : undefined
  const calls = frames.map((text, index) => {
    const frame = call('image_generate', text)
    frame.dag = {
      title: `生成关键帧 ${index + 1}`,
      capabilityId: 'image.text_to_image',
      dependsOn: cutout ? [cutout.id] : [],
      targetNodes: [`output:${frame.id}`],
      tier: 'preview',
      parallelGroup: 'keyframes',
    }
    return frame
  })
  const compose = call('frames_to_video', prompt, { frameCallIds: calls.map((c) => c.id) })
  compose.dag = {
    title: '合成视频',
    capabilityId: 'video.frames_to_video',
    dependsOn: calls.map((c) => c.id),
    targetNodes: [`output:${compose.id}`],
    tier: 'preview',
  }
  return [...(cutout ? [cutout] : []), ...calls, compose]
}
export class PlanValidator {
  validate(
    intent: IntentResult,
    calls: ToolCall[],
    options: RoutingOptions,
    registered: readonly string[],
  ): PlanValidation {
    const errors: string[] = []
    try {
      errors.push(...validateDag(executionDag(calls)))
      for (const call of calls) {
        if (!registered.includes(call.name)) {
          errors.push(`工具 ${call.name} 未注册`)
        }
        if (call.dag && call.dag.capabilityId !== (toolCapabilities[call.name] ?? call.name)) {
          errors.push(`步骤 ${call.id} 的能力与工具不匹配`)
        }
      }
    } catch {
      errors.push('计划 DAG 字段不符合契约')
    }
    if (intent.path === 'clarify') {
      return { valid: false, code: 'plan_invalid', errors: ['意图尚待用户澄清'] }
    }
    if (intent.goal !== 'video') {
      return {
        valid: errors.length === 0,
        ...(errors.length ? { code: 'plan_invalid' as const } : {}),
        errors,
      }
    }
    if (
      !options.videoToolEnabled ||
      (intent.path === 'image_to_multiframe' && !options.multiframeEnabled) ||
      (intent.path === 'image_to_video' && !options.directI2vEnabled)
    ) {
      return { valid: false, code: 'video_unavailable', errors: ['所选视频路径未启用'] }
    }
    if (new Set(calls.map((call) => call.id)).size !== calls.length) {
      return { valid: false, code: 'plan_invalid', errors: ['计划工具调用 ID 必须唯一'] }
    }
    const names = calls.map((c) => c.name)
    if (names.some((name) => !registered.includes(name))) {
      return { valid: false, code: 'video_unavailable', errors: ['所选路径工具未注册'] }
    }
    if (intent.path === 'text_to_video' && (names.length !== 1 || names[0] !== 'video_generate')) {
      errors.push('文生视频模板必须以 video_generate 交付')
    }
    if (intent.path === 'image_to_video' && (names.length !== 1 || names[0] !== 'image_to_video')) {
      errors.push('直接图生视频模板必须交付视频')
    }
    if (intent.path === 'image_to_multiframe') {
      const end = names.at(-1)
      const preceding = names.slice(0, -1)
      if (
        !['frames_to_video', 'image_to_video'].includes(end ?? '') ||
        preceding.filter((name) => imageNames.includes(name)).length < 2 ||
        preceding.filter((name) => imageNames.includes(name)).length >
          (options.maxFrameCount ?? 8) ||
        (intent.frameCount !== undefined &&
          preceding.filter((name) => imageNames.includes(name)).length !== intent.frameCount) ||
        preceding.some(
          (name, i) =>
            !imageNames.includes(name) && !(['cutout', 'subject_cutout'].includes(name) && i === 0),
        )
      ) {
        errors.push('多帧模板必须至少生成两帧并以视频合成交付，禁止单图结束')
      }
    }
    if (intent.path === 'image_to_multiframe') {
      const end = calls.at(-1)
      if (end) {
        const graph = executionDag(calls)
        const dependencies = new Set<string>()
        const visit = (id: string) => {
          if (dependencies.has(id)) {
            return
          }
          dependencies.add(id)
          for (const dep of graph.find((step) => step.id === id)?.dependsOn ?? []) {
            visit(dep)
          }
        }
        visit(end.id)
        if (
          calls
            .filter((call) => imageNames.includes(call.name))
            .some((call) => !dependencies.has(call.id))
        ) {
          errors.push('视频合成必须依赖所有关键帧')
        }
      }
    }
    return {
      valid: errors.length === 0,
      ...(errors.length ? { code: 'plan_invalid' as const } : {}),
      errors,
    }
  }
  enforce(validation: PlanValidation) {
    if (!validation.valid) {
      throw new RoutingError(validation.code ?? 'plan_invalid', validation.errors.join('；'))
    }
  }
}

/** Four views are four independent outputs, never concurrent writes to the source. */
export function fourDirectionTemplate(
  prompt: string,
  imageRefs: string[],
  registered: readonly string[],
): ToolCall[] | undefined {
  if (!/四(?:个)?方向|前后左右|四(?:个)?视角/.test(prompt)) {
    return undefined
  }
  const name = imageNames.find((candidate) => registered.includes(candidate))
  if (!name) {
    throw new RoutingError('plan_invalid', '四方向生图能力未注册')
  }
  return ['正面', '背面', '左侧', '右侧'].map((direction) => {
    const id = randomUUID()
    return {
      id,
      name,
      args: { prompt: `${prompt}；视角：${direction}`, imageRefs },
      dag: {
        title: `生成${direction}画面`,
        capabilityId: 'image.text_to_image',
        dependsOn: [],
        targetNodes: [`output:${id}`],
        tier: 'preview',
        parallelGroup: 'four-directions',
      },
    }
  })
}
