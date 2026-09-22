import { ensureAsset } from './asset-util.js'
import type { DocStore } from '../state/doc-store.js'
import { cmdBatch, cmdUpdateObject, type CanvasObj } from '../state/commands.js'
import { isBusy, isMediaNode } from '../state/node-model.js'

export type SubjectApplyResult = {
  applied: number
  skipped: string[]
}

/**
 * 把选定图片作为主体参考，应用到全部待生成的未锁定镜头节点，保证跨镜头主体一致。
 * 服务端当前仅支持一张参考图：已有参考或已有任务记录的镜头跳过并说明原因。
 * 整批为一条可撤销命令；异步入库期间分镜、主体或节点变化则放弃全部写入。
 */
export async function applySubjectToShots(
  store: DocStore,
  subject: CanvasObj | undefined,
  signal: AbortSignal,
  importAsset: typeof ensureAsset = ensureAsset,
): Promise<SubjectApplyResult> {
  if (!isMediaNode(subject) || !(subject.src || subject.assetId)) {
    throw new Error('请先选择一张已生成或素材库中的图片作为主体')
  }
  if (subject.kind !== 'image') {
    throw new Error('主体参考目前仅支持图片素材')
  }
  const boardSnapshot = JSON.stringify(store.doc.storyboard)
  const subjectSnapshot = {
    id: subject.id,
    src: subject.src,
    assetId: subject.assetId,
    ext: subject.ext,
  }
  const collect = () => {
    const targets: { nodeId: string; draft: object }[] = []
    const skipped: string[] = []
    ;(store.doc.storyboard?.shots || []).forEach((shot, index) => {
      const label = `镜头 ${index + 1}「${shot.title}」`
      if (shot.locked) {
        skipped.push(`${label}已锁定`)
        return
      }
      const node = shot.nodeId ? store.doc.objects[shot.nodeId] : undefined
      if (!node || !isMediaNode(node)) {
        skipped.push(`${label}没有可生成的节点`)
        return
      }
      if (node.src || node.assetId) {
        skipped.push(`${label}已有素材`)
        return
      }
      if (node.nodeRun || isBusy(node)) {
        skipped.push(`${label}已有任务或待处理结果`)
        return
      }
      if (node.nodeDraft!.references.length) {
        skipped.push(`${label}已携带参考图`)
        return
      }
      targets.push({ nodeId: node.id, draft: structuredClone(node.nodeDraft!) })
    })
    return { targets, skipped }
  }
  if (!collect().targets.length) {
    throw new Error('没有可应用主体的待生成镜头节点')
  }
  // 已在素材库中的主体直接复用 assetId，不重复入库
  const asset = subject.assetId
    ? { id: subject.assetId, ext: subject.ext || 'png' }
    : await importAsset(subject, signal)
  if (
    signal.aborted ||
    JSON.stringify(store.doc.storyboard) !== boardSnapshot ||
    !store.doc.objects[subject.id] ||
    subject.src !== subjectSnapshot.src ||
    subject.assetId !== subjectSnapshot.assetId ||
    subject.ext !== subjectSnapshot.ext
  ) {
    throw new Error('分镜或主体素材已变化，请重新操作')
  }
  // 重新收集：异步入库期间节点可能已生成、已提交或已携带参考
  const { targets, skipped } = collect()
  if (!targets.length) {
    throw new Error('镜头或节点已变化，没有可应用主体的待生成镜头')
  }
  const commands = targets.map(({ nodeId, draft }) =>
    cmdUpdateObject(
      nodeId,
      {
        nodeDraft: {
          ...(draft as Record<string, unknown>),
          references: [
            {
              assetId: asset.id,
              ext: asset.ext,
              name: subject.name || '主体参考',
              sourceNodeId: subject.id,
            },
          ],
        },
      },
      { nodeDraft: draft },
    ),
  )
  store.apply(cmdBatch(commands))
  return { applied: targets.length, skipped }
}
