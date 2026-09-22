import type { DocStore } from '../state/doc-store.js'
import { cmdUpdateObject } from '../state/commands.js'
import type { Capabilities } from '../state/node-model.js'
import { ensureAsset } from './asset-util.js'

export async function connectReference(
  store: DocStore,
  sourceId: string,
  targetId: string,
  signal: AbortSignal,
) {
  const source = store.doc.objects[sourceId]
  const target = store.doc.objects[targetId]
  if (
    !source ||
    !target ||
    source === target ||
    source.kind !== 'image' ||
    !(source.assetId || source.src) ||
    !target.nodeDraft ||
    target.nodeRun ||
    target.assetId ||
    target.src ||
    !['image', 'video'].includes(target.kind)
  ) {
    throw new Error('请将已有图片连接到空的图片或视频节点')
  }
  const draft = target.nodeDraft
  const sourceAsset = source.assetId
  const sourceUrl = source.src
  const response = await fetch('/api/generation-capabilities', { signal })
  if (!response.ok) {
    throw new Error('无法读取模型参考能力，请重试')
  }
  const capabilities = (await response.json()) as Capabilities
  const cap = capabilities[target.kind as 'image' | 'video']
  if (!cap?.supported || !Number.isInteger(cap.referenceLimit) || cap.referenceLimit < 1) {
    throw new Error('当前模型不支持参考图')
  }
  if (
    target.nodeDraft.references.some(
      (ref) =>
        ref.sourceNodeId === sourceId || (!!source.assetId && ref.assetId === source.assetId),
    )
  ) {
    throw new Error('该图片已被引用')
  }
  if (target.nodeDraft.references.length >= cap.referenceLimit) {
    throw new Error(`当前模型最多支持 ${cap.referenceLimit} 张参考图`)
  }
  const asset = await ensureAsset(source, signal)
  if (
    signal.aborted ||
    store.doc.objects[sourceId] !== source ||
    store.doc.objects[targetId] !== target ||
    target.nodeRun ||
    target.src ||
    target.assetId ||
    target.nodeDraft !== draft ||
    source.assetId !== sourceAsset ||
    source.src !== sourceUrl
  ) {
    throw new Error('节点已变化，请重新连接')
  }
  store.apply(
    cmdUpdateObject(
      target.id,
      {
        nodeDraft: {
          ...target.nodeDraft,
          references: [
            ...target.nodeDraft.references,
            {
              assetId: asset.id,
              ext: asset.ext,
              name: source.name || '画布图片',
              sourceNodeId: sourceId,
            },
          ],
        },
      },
      { nodeDraft: target.nodeDraft },
    ),
  )
}
