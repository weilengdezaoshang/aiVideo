// T18 草稿模式与种子锁定的纯参数规则:低步数小尺寸出小样,满意后原参数高配重出。

export const DRAFT_STEPS = 8
export const FULL_STEPS = 20
const MIN_SIDE = 64
const MAX_SIDE = 2048

const align8 = (value: number): number =>
  Math.max(MIN_SIDE, Math.min(MAX_SIDE, Math.round(value / 8) * 8))

export type GenerationSize = { width: number; height: number }

/** 草稿模式:边长减半(8 对齐),用于先出小样。 */
export function draftSize(size: GenerationSize): GenerationSize {
  return { width: align8(size.width / 2), height: align8(size.height / 2) }
}

/** 判定一份谱系参数是否草稿小样:步数不高于草稿档即视为草稿。 */
export function isDraftParams(params: Record<string, unknown>): boolean {
  return typeof params.steps === 'number' && params.steps > 0 && params.steps <= DRAFT_STEPS
}

/** 高配重出:恢复默认步数,边长翻倍回原档(草稿减半的逆操作)。 */
export function fullQualityParams(params: Record<string, unknown>): Record<string, unknown> {
  const width = Number(params.width)
  const height = Number(params.height)
  const size =
    width > 0 && height > 0 ? { width: align8(width * 2), height: align8(height * 2) } : {}
  return { ...params, steps: FULL_STEPS, ...size }
}

/** 种子锁定:锁定的种子必须是非负整数;未启用或留空(随机)交回 -1。 */
export function lockedSeed(enabled: boolean, raw: string): number {
  if (!enabled || !raw.trim()) {
    return -1
  }
  const value = Number(raw)
  return Number.isInteger(value) && value >= 0 && value < 2 ** 31 ? value : -1
}

/** 从谱系参数读取可锁定的种子;历史任务(受理时未固化)没有可用种子。 */
export function seedOf(params: unknown): number | null {
  if (!params || typeof params !== 'object') {
    return null
  }
  const value = (params as Record<string, unknown>).seed
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null
}
