import { StubReviewerProvider, type ReflectionOptions } from '../../agent/reflection.js'
import type { ReviewerProvider } from '../../ports/index.js'
import { RealReviewer } from './real.js'

export interface ReviewerConfig {
  mode?: string
  real?: { model?: string; api?: string; timeoutMs?: number }
}
export function createReviewer(
  config: ReviewerConfig = {},
  env: NodeJS.ProcessEnv = process.env,
): ReviewerProvider {
  // 非法模式明确落到 stub；只有显式 real 才构造真实适配器。
  if (config.mode !== 'real') {
    return new StubReviewerProvider()
  }
  return new RealReviewer({
    model: config.real?.model ?? env.REAL_REVIEWER_MODEL,
    api: config.real?.api ?? env.REAL_REVIEWER_API,
    timeoutMs: config.real?.timeoutMs ?? Number(env.REAL_REVIEWER_TIMEOUT_MS ?? 15000),
    apiKey: env.REAL_REVIEWER_API_KEY,
  })
}
export function reviewerOptionsFromEnv(
  env: NodeJS.ProcessEnv = process.env,
): ReflectionOptions & { reviewer: ReviewerConfig } {
  let rubric: ReflectionOptions['rubric']
  if (env.REVIEW_RUBRIC) {
    const value: unknown = JSON.parse(env.REVIEW_RUBRIC)
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      throw new Error('REVIEW_RUBRIC 必须是 image/video 字符串数组对象')
    }
    const parsed = value as Record<string, unknown>
    rubric = {}
    for (const key of Object.keys(parsed)) {
      const items = parsed[key]
      if (
        !['image', 'video'].includes(key) ||
        !Array.isArray(items) ||
        !items.length ||
        !items.every((item: unknown) => typeof item === 'string' && !!item.trim())
      ) {
        throw new Error('REVIEW_RUBRIC 必须是 image/video 非空字符串数组对象')
      }
      rubric[key as 'image' | 'video'] = items as string[]
    }
  }
  return {
    reviewEnabled: env.REVIEW_ENABLED !== 'false',
    reviewThreshold: Number(env.REVIEW_THRESHOLD ?? 0.7),
    maxReviewRetry: Number(env.MAX_REVIEW_RETRY ?? 2),
    retryCredits: env.RETRY_CREDITS !== 'false',
    rubric,
    reviewer: {
      mode: env.REVIEWER_MODE ?? 'stub',
      real: {
        model: env.REAL_REVIEWER_MODEL,
        api: env.REAL_REVIEWER_API,
        timeoutMs: Number(env.REAL_REVIEWER_TIMEOUT_MS ?? 15000),
      },
    },
  }
}
