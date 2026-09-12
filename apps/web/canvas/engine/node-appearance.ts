import type { CanvasObj } from '../state/commands.js'
import { hasOutput, isMediaNode } from '../state/node-model.js'

export function isEmptyMedia(obj: CanvasObj): boolean {
  return isMediaNode(obj) && !hasOutput(obj)
}
export function nodeAppearanceKey(obj: CanvasObj): string {
  return JSON.stringify([isEmptyMedia(obj), obj.textCard, obj.backgroundColor])
}
export function nodeStatusLabel(obj: CanvasObj): string {
  if (obj.nodeRun) {
    const labels = {
      submitting: '提交中',
      queued: '排队中',
      running: '生成中',
      failed: '生成失败 · 点击重试',
      uncertain: '等待确认结果',
    }
    return labels[obj.nodeRun.status]
  }
  return obj.kind === 'video' ? '视频 · 待创建' : '图片 · 待创建'
}

/** 即梦参考中的不透明斜向渐变；自定义文本背景继续使用文档中的颜色。 */
export function placeholderSurface(
  obj: CanvasObj,
  dark: boolean,
): {
  fill: string
  fillPriority: 'color' | 'linear-gradient'
  fillLinearGradientStartPoint: { x: number; y: number }
  fillLinearGradientEndPoint: { x: number; y: number }
  fillLinearGradientColorStops: (number | string)[]
  stroke: string
} {
  const custom = obj.textCard ? obj.backgroundColor : undefined
  return {
    fill: custom || (dark ? '#222222' : '#f5f5f5'),
    fillPriority: custom ? 'color' : 'linear-gradient',
    fillLinearGradientStartPoint: { x: 0, y: 0 },
    fillLinearGradientEndPoint: { x: obj.width, y: obj.height },
    fillLinearGradientColorStops: dark
      ? [0, '#222222', 1, '#141414']
      : [0, '#f5f5f5', 1, '#ebebeb'],
    stroke: dark ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.06)',
  }
}

/** Progress is reported by the task pipeline, never interpolated on the client. */
export function nodeProgressLabel(obj: CanvasObj): string {
  const run = obj.nodeRun
  if (!run) {
    return ''
  }
  if (run.status !== 'running') {
    return nodeStatusLabel(obj)
  }
  const progress = run.progress
  if (progress == null || !Number.isFinite(progress)) {
    return '生成中…'
  }
  return `任务进度 ${Math.floor(Math.max(0, Math.min(0.99, progress)) * 100)}%`
}
