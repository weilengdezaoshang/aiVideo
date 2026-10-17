import type { CanvasView } from '../core/canvas-view.js'
import type { AgentRun } from '../agent/types.js'

export type AgentCutoutOwner = { runId: string; sourceObjectId: string; sourceAssetId: string }

/** A fresh response never steals a later selection; historical actions require a click. */
export function openAgentCutout(input: {
  run: AgentRun
  automatic: boolean
  view: CanvasView
  applying: boolean
  activeRunId?: string
  start: (sourceId: string, owner: AgentCutoutOwner) => void
}): void {
  const { run, view } = input
  const action = run.canvasAction
  if (!action || action.type !== 'cutout' || run.status !== 'waiting_approval') {
    return
  }
  if (action.documentId !== view.getDocument().id) {
    throw new Error('抠图任务不属于当前画布，请返回原画布继续')
  }
  const source = view.getDocument().objects[action.sourceObjectId]
  if (!source || source.kind !== 'image' || source.assetId !== action.sourceAssetId) {
    throw new Error('抠图源图已变化，请选中当前图片重新发送要求')
  }
  if (input.applying) {
    throw new Error('正在提交抠图，请完成后再进入其他抠图任务')
  }
  const current = view.getSnapshot()
  if (input.automatic && (current.selection.length !== 1 || current.selection[0] !== source.id)) {
    throw new Error('选区已变化，请点击对话中的「进入抠图模式」继续')
  }
  if (input.activeRunId === run.id && ['mask', 'recognizing'].includes(current.requestedTool)) {
    return
  }
  view.select([source.id])
  view.focusObject(source.id)
  input.start(source.id, {
    runId: run.id,
    sourceObjectId: source.id,
    sourceAssetId: action.sourceAssetId,
  })
}
