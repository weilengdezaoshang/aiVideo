import type { CanvasObj } from '../state/commands.js'
import type { Capability } from '../state/node-model.js'
import { ensureAsset } from './asset-util.js'

/** 与服务端 referenceWeight 契约一致的参考强度枚举 */
export type ReferenceWeight = 'low' | 'medium' | 'high'

export type FormatOption = { value: string; label: string }

/** 输入卡引用 → 服务端资产 id;引用对象已删除时明确报错,不静默降级为文生图。 */
export async function resolveComposerReference(
  obj: CanvasObj | undefined,
): Promise<{ assetId: string }> {
  if (!obj || obj.kind !== 'image') {
    throw new Error('引用的画布图片不存在或已删除,请重新添加引用')
  }
  if (obj.assetId) {
    return { assetId: obj.assetId }
  }
  const asset = await ensureAsset(obj)
  return { assetId: asset.id }
}

/** 读取参考程度下拉;值不在契约枚举内时返回 undefined(不随 payload 上送)。 */
export function readReferenceWeight(select: HTMLSelectElement | null): ReferenceWeight | undefined {
  const value = select?.value
  return value === 'low' || value === 'medium' || value === 'high' ? value : undefined
}

function ratioLabel(width: number, height: number): string {
  const gcd = (a: number, b: number): number => (b === 0 ? a : gcd(b, a % b))
  const divisor = gcd(width, height) || 1
  return `${Math.round(width / divisor)}:${Math.round(height / divisor)}`
}

/**
 * 视频画幅下拉:云端优先用能力契约下发的精确 sizes(厂商只接受固定档位);
 * 本地后端无 sizes 时回退 ratios×resolutions 组合(8 像素对齐,与 outputSize 一致)。
 */
export function videoFormatOptions(video: Capability): FormatOption[] {
  if (video.sizes?.length) {
    return video.sizes.map((size) => {
      const [w, h] = size.split('x').map(Number)
      return { value: size, label: `${w}×${h}(${ratioLabel(w, h)})` }
    })
  }
  const options: FormatOption[] = []
  for (const ratio of video.ratios) {
    for (const resolution of video.resolutions) {
      const [a, b] = ratio.split(':').map(Number)
      const horizontal = a >= b
      const width = Math.max(
        64,
        Math.round((horizontal ? resolution : (resolution * a) / b) / 8) * 8,
      )
      const height = Math.max(
        64,
        Math.round((horizontal ? (resolution * b) / a : resolution) / 8) * 8,
      )
      options.push({ value: `${width}x${height}`, label: `${ratio} · ${resolution}` })
    }
  }
  return options
}

/** 视频时长下拉:能力未下发时回退 4 秒(与 GenParams 缺省一致)。 */
export function videoDurationOptions(video: Capability): FormatOption[] {
  const durations = video.durations?.length ? video.durations : [4]
  return durations.map((seconds) => ({ value: String(seconds), label: `${seconds} 秒` }))
}

/** 用选项列表填充下拉;保留当前选中值(仍存在时)。 */
export function fillSelect(select: HTMLSelectElement, options: FormatOption[]): void {
  const current = select.value
  select.replaceChildren(
    ...options.map((option) => {
      const element = document.createElement('option')
      element.value = option.value
      element.textContent = option.label
      return element
    }),
  )
  if (options.some((option) => option.value === current)) {
    select.value = current
  }
}
