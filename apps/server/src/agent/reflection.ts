import { z } from 'zod'
import type { GeneratedToolResult } from '../domain/model.js'
import type { ReviewContext, ReviewerProvider } from '../ports/index.js'

export const evidenceSchema = z.object({
  checks: z.array(
    z.object({
      name: z.string(),
      pass: z.boolean(),
      expected: z.unknown().optional(),
      actual: z.unknown().optional(),
    }),
  ),
  review: z
    .object({
      score: z.number().min(0).max(1),
      rubric: z.array(
        z.object({ item: z.string(), pass: z.boolean(), note: z.string().optional() }),
      ),
      issues: z.array(z.string()),
      attempt: z.number().int().positive(),
      reviewer: z.string(),
    })
    .optional(),
})
export type Evidence = z.infer<typeof evidenceSchema>
export interface ReflectionOptions {
  reviewEnabled?: boolean
  reviewThreshold?: number
  maxReviewRetry?: number
  retryCredits?: boolean
  rubric?: Partial<Record<'image' | 'video', string[]>>
}
export function validateReflectionOptions(options: ReflectionOptions) {
  const threshold = options.reviewThreshold ?? 0.7
  const retries = options.maxReviewRetry ?? 2
  if (
    !Number.isFinite(threshold) ||
    threshold < 0 ||
    threshold > 1 ||
    !Number.isSafeInteger(retries) ||
    retries < 0
  ) {
    throw new Error('审核阈值必须在 0～1，重试上限必须是非负整数')
  }
}
export function runHardChecks(result: GeneratedToolResult): Evidence['checks'] {
  const metadata = result.metadata
  return [
    {
      name: 'artifact',
      pass: result.artifact.status === 'success' && !!result.artifact.url,
      expected: 'success',
      actual: result.artifact.status,
    },
    {
      name: 'non_empty',
      pass: metadata ? metadata.bytes > 0 : !!result.artifact.url,
      expected: '> 0',
      actual: metadata?.bytes ?? 'provider URL',
    },
    ...(metadata
      ? [
          {
            name: 'resolution',
            pass:
              metadata.width > 0 &&
              metadata.height > 0 &&
              (!metadata.expectedWidth || metadata.width === metadata.expectedWidth) &&
              (!metadata.expectedHeight || metadata.height === metadata.expectedHeight),
            expected: [metadata.expectedWidth ?? '> 0', metadata.expectedHeight ?? '> 0'],
            actual: [metadata.width, metadata.height],
          },
          {
            name: 'aspect_ratio',
            pass:
              !metadata.expectedRatio ||
              Math.abs(metadata.width / metadata.height - metadata.expectedRatio) < 0.01,
            expected: metadata.expectedRatio,
            actual: metadata.width / metadata.height,
          },
        ]
      : []),
  ]
}
/** 多模态适配器可通过 ReviewerProvider 替换；默认仅使用明确的 stub 夹具标记。 */
export class StubReviewerProvider implements ReviewerProvider {
  async review(input: Parameters<ReviewerProvider['review']>[0]) {
    input.signal.throwIfAborted()
    const pass = input.result.metadata?.reviewPass !== false
    return {
      score: pass ? 0.9 : 0.3,
      rubric: input.rubric.map((item) => ({
        item,
        pass,
        note: pass ? '夹具通过' : '夹具质量不达标',
      })),
      issues: pass ? [] : ['画面不符合创作要求'],
      attempt: input.attempt,
      reviewer: 'stub',
    }
  }
}
export async function finalizeGeneration(
  result: GeneratedToolResult,
  attempt: number,
  reviewer: ReviewerProvider,
  options: ReflectionOptions,
  signal: AbortSignal,
  prompt = '',
  context: ReviewContext = {},
) {
  if (options.reviewEnabled === false) {
    return { outcome: 'accepted' as const }
  }
  const checks = runHardChecks(result)
  const rubric =
    options.rubric?.[result.artifact.type] ??
    (result.artifact.type === 'image'
      ? ['相关性', '明显残缺', '比例']
      : ['主题符合要求', '时序连贯'])
  const review = await reviewer.review({
    artifact: {
      url: result.artifact.url,
      localPath: result.metadata?.localPath,
      fixtureId: result.metadata?.fixtureId,
    },
    result,
    prompt,
    taskType: result.artifact.type,
    rubricId: `${result.artifact.type}-quality-v1`,
    attempt,
    rubric,
    context,
    signal,
  })
  signal.throwIfAborted()
  const evidence = evidenceSchema.parse({ checks, review })
  const issues = [
    ...checks.filter((check) => !check.pass).map((check) => `硬检查未通过：${check.name}`),
    ...review.issues,
  ]
  const pass =
    checks.every((check) => check.pass) &&
    review.score >= (options.reviewThreshold ?? 0.7) &&
    rubric.length > 0 &&
    rubric.every((item) => review.rubric.some((entry) => entry.item === item && entry.pass)) &&
    review.rubric.every((item) => item.pass)
  if (!pass && !issues.length) {
    issues.push('审核评分或关键项未达标')
  }
  evidence.review!.issues = issues
  result.evidence = evidence
  return {
    outcome: pass
      ? ('accepted' as const)
      : attempt <= (options.maxReviewRetry ?? 2)
        ? ('retried' as const)
        : ('escalated' as const),
    evidence,
  }
}
