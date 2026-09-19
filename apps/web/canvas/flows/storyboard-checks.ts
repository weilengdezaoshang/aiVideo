import { isBusy } from '../state/node-model.js'
import type { CanvasObj } from '../state/commands.js'
import type { Storyboard } from '../state/storyboard.js'
import type { Timeline } from '../state/timeline.js'

export type StoryboardIssue = {
  severity: 'error' | 'warning'
  message: string
}

/**
 * 连续性与质量检查：全部为本地可验证的静态检查，逐条给出含镜头号的解释。
 * error = 会阻断执行、装配或导出；warning = 提示，不阻断。
 */
export function runStoryboardChecks(
  board: Storyboard | undefined,
  objects: Record<string, CanvasObj>,
  timeline: Timeline | undefined,
): StoryboardIssue[] {
  const issues: StoryboardIssue[] = []
  if (!board || !board.shots.length) {
    return [{ severity: 'warning', message: '尚未创建分镜镜头' }]
  }
  board.shots.forEach((shot, index) => {
    const label = `镜头 ${index + 1}「${shot.title}」`
    if (!shot.visual.trim()) {
      issues.push({ severity: 'warning', message: `${label}画面描述为空，生成会缺少依据` })
    }
    if (!shot.nodeId) {
      issues.push({ severity: 'warning', message: `${label}尚未关联生成节点` })
      return
    }
    const node = objects[shot.nodeId]
    if (!node) {
      issues.push({ severity: 'error', message: `${label}关联节点已删除，请重新关联或创建节点` })
      return
    }
    if (node.src || node.assetId) {
      return
    }
    if (node.nodeRun?.status === 'failed') {
      issues.push({
        severity: 'error',
        message: `${label}生成失败：${node.nodeRun.message || '请重试该镜头'}`,
      })
    } else if (node.nodeRun?.status === 'uncertain') {
      issues.push({ severity: 'error', message: `${label}生成结果待确认，请先处理该节点任务` })
    } else if (isBusy(node)) {
      issues.push({ severity: 'warning', message: `${label}正在生成中` })
    } else {
      issues.push({ severity: 'warning', message: `${label}尚未生成素材` })
    }
  })
  const timelineClips = timeline?.clips || []
  timelineClips.forEach((clip, index) => {
    const label = `片段 ${index + 1}「${clip.name}」`
    const url = clip.source.url
    if (!url.startsWith('/images/') && !url.startsWith('/assets/')) {
      issues.push({
        severity: 'error',
        message: `${label}素材来自外部地址，导出前需先导入素材库`,
      })
    }
    if (clip.subtitle && clip.subtitle.trim() && clip.outFrame - clip.inFrame < 45) {
      issues.push({
        severity: 'warning',
        message: `${label}时长不足 1.5 秒但携带台词，字幕可能一闪而过`,
      })
    }
  })
  if (timelineClips.length) {
    const clipNodes = new Set(timelineClips.map((clip) => clip.source.nodeId))
    const missing = board.shots
      .map((shot, index) => ({ shot, index }))
      .filter(({ shot }) => {
        if (!shot.nodeId) {
          return false
        }
        const node = objects[shot.nodeId]
        return !!node && !!(node.src || node.assetId) && !clipNodes.has(shot.nodeId)
      })
    if (missing.length) {
      issues.push({
        severity: 'warning',
        message: `镜头 ${missing.map((item) => item.index + 1).join('、')} 已生成但尚未进入时间线，可重新装配或手动调整`,
      })
    }
  }
  return issues
}
