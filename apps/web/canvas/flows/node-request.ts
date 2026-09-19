import type { CanvasObj } from '../state/commands.js'
import {
  hasOutput,
  isMediaNode,
  outputSize,
  type Capability,
  type NodeDraft,
} from '../state/node-model.js'

/** 只准备与校验，不提交任务，也不修改传入节点。 */
export function prepareNodeDraft(obj: CanvasObj, cap: Capability | undefined): NodeDraft {
  if (!isMediaNode(obj) || hasOutput(obj)) {
    throw new Error('请先为镜头创建新的生成节点')
  }
  if (!cap?.supported) {
    throw new Error('当前模型不支持此类生成')
  }
  const draft = structuredClone(obj.nodeDraft)
  if (!cap.models.some((model) => model.id === draft.model)) {
    draft.model = cap.models[0]?.id || ''
  }
  if (!cap.ratios.includes(draft.ratio)) {
    draft.ratio = cap.ratios[0] || ''
  }
  if (!cap.resolutions.includes(draft.resolution)) {
    draft.resolution = cap.resolutions[0] || 0
  }
  if (
    !draft.prompt.trim() ||
    draft.prompt.length > 4000 ||
    !draft.model ||
    !draft.ratio ||
    !draft.resolution
  ) {
    throw new Error('请检查节点描述、模型和画幅')
  }
  if (draft.references.length > cap.referenceLimit) {
    throw new Error('参考图数量超过当前模型上限')
  }
  if (obj.kind === 'video' && cap.durations?.length && !cap.durations.includes(draft.durationSec)) {
    throw new Error('请打开节点选择模型支持的视频时长')
  }
  return draft
}

export function nodeRequest(
  documentId: string,
  obj: CanvasObj,
  requestId: string,
  draft: NodeDraft,
) {
  return {
    documentId,
    clientRef: obj.id,
    requestId,
    kind: obj.kind,
    prompt: draft.prompt,
    model: draft.model,
    ...outputSize(draft),
    durationSec: draft.durationSec,
    fps: 16,
    denoise: draft.denoise,
    batchCount: 1,
    steps: 20,
    cfgScale: 7,
    seed: -1,
    sampler: 'euler',
    scheduler: 'normal',
    referenceAssetIds: draft.references.map((reference) => reference.assetId),
  }
}
