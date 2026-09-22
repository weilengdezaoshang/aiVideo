import { ensureAsset } from './asset-util.js'
import type { DocStore } from '../state/doc-store.js'
import { cmdAddObjects, cmdBatch } from '../state/commands.js'
import { createNode, type MediaKind } from '../state/node-model.js'

/** 创建可编辑生成草稿；一次撤销同时恢复镜头关联与画布。 */
function createShotNodes(
  store: DocStore,
  shotIds: string[],
  kind: MediaKind,
  references = new Map<string, { id: string; ext: string }>(),
): string[] {
  const board = store.doc.storyboard
  if (!board || !shotIds.length) {
    throw new Error('没有需要创建节点的镜头')
  }
  const next = structuredClone(board)
  const existing = Object.values(store.doc.objects)
  const right = existing.length ? Math.max(...existing.map((obj) => obj.x + obj.width)) + 80 : 80
  const created = []
  for (const [index, shotId] of shotIds.entries()) {
    const shot = board.shots.find((item) => item.id === shotId)
    if (!shot || shot.locked) {
      throw new Error('镜头不存在或已锁定')
    }
    if (!shot.visual.trim()) {
      throw new Error(`镜头「${shot.title || '未命名'}」缺少画面描述`)
    }
    const versions = [...new Set([...(shot.versions || []), ...(shot.nodeId ? [shot.nodeId] : [])])]
    if (versions.length >= 100) {
      throw new Error('该镜头已达到 100 个版本上限')
    }
    const node = createNode(
      kind,
      { x: right + (index % 4) * 440, y: 80 + Math.floor(index / 4) * 420 },
      shot.title || '分镜',
    )
    node.nodeDraft!.prompt = [
      shot.visual.trim(),
      shot.camera.trim() ? `运镜：${shot.camera.trim()}` : '',
    ]
      .filter(Boolean)
      .join('\n')
    if (node.nodeDraft!.prompt.length > 4000) {
      throw new Error('画面描述与运镜合计超过 4000 字，请缩短后创建节点')
    }
    node.nodeDraft!.durationSec = shot.durationFrames / 30
    const previous = shot.nodeId ? store.doc.objects[shot.nodeId] : undefined
    const reference = previous ? references.get(previous.id) : undefined
    if (previous?.kind === 'image' && (previous.assetId || reference)) {
      node.nodeDraft!.references = [
        {
          assetId: reference?.id || previous.assetId!,
          ext: reference?.ext || previous.ext || 'png',
          name: previous.name || '镜头参考',
          sourceNodeId: previous.id,
        },
      ]
    }
    const nextShot = next.shots.find((item) => item.id === shot.id)!
    nextShot.nodeId = node.id
    nextShot.versions = [...versions, node.id]
    created.push(node)
  }
  store.apply(
    cmdBatch([cmdAddObjects(created), { type: 'setStoryboard', before: board, after: next }]),
  )
  return created.map((node) => node.id)
}

export function createShotNode(store: DocStore, shotId: string, kind: MediaKind): string {
  return createShotNodes(store, [shotId], kind)[0]
}

/** 仅补齐未关联或关联已删除的未锁定镜头，不替换现有素材。 */
export function createMissingShotNodes(store: DocStore, kind: MediaKind): string[] {
  const shots = store.doc.storyboard?.shots || []
  return createShotNodes(
    store,
    shots
      .filter((shot) => !shot.locked && (!shot.nodeId || !store.doc.objects[shot.nodeId]))
      .map((shot) => shot.id),
    kind,
  )
}

/** 从已完成图片准备视频，异步入库期间修改分镜会取消整批写入。 */
export async function createShotVideoVersions(
  store: DocStore,
  signal: AbortSignal,
  importAsset: typeof ensureAsset = ensureAsset,
): Promise<string[]> {
  const board = store.doc.storyboard
  const snapshot = JSON.stringify(board)
  const shots = (board?.shots || []).filter((shot) => {
    const node = shot.nodeId ? store.doc.objects[shot.nodeId] : undefined
    return !shot.locked && node?.kind === 'image' && !node.nodeRun && !!(node.assetId || node.src)
  })
  if (!shots.length) {
    throw new Error('没有可转为视频的已完成图片镜头')
  }
  const sources = shots.map((shot) => {
    const node = store.doc.objects[shot.nodeId!]!
    return { node, src: node.src, assetId: node.assetId, ext: node.ext }
  })
  const references = new Map<string, { id: string; ext: string }>()
  const validate = () => {
    if (
      signal.aborted ||
      JSON.stringify(store.doc.storyboard) !== snapshot ||
      sources.some(
        ({ node, src, assetId, ext }) =>
          store.doc.objects[node.id] !== node ||
          node.src !== src ||
          node.assetId !== assetId ||
          node.ext !== ext ||
          node.nodeRun,
      )
    ) {
      throw new Error('分镜或图片已变化，已停止创建视频节点，请重新操作')
    }
  }
  for (const { node } of sources) {
    validate()
    if (!references.has(node.id)) {
      references.set(node.id, await importAsset(node, signal))
    }
  }
  validate()
  return createShotNodes(
    store,
    shots.map((shot) => shot.id),
    'video',
    references,
  )
}
