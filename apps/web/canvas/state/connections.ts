import type { CanvasObj } from './commands.js'
import { transformPoint, type Geometry } from '../engine/transform-geometry.js'

export type CanvasConnection = {
  sourceId: string
  targetId: string
  kind: 'reference' | 'lineage'
}

/** 引用和生成来源是唯一数据源；不另存一套容易失配的连线。 */
export function documentConnections(objects: Record<string, CanvasObj>): CanvasConnection[] {
  const edges: CanvasConnection[] = []
  for (const target of Object.values(objects)) {
    const sources = new Set<string>()
    const add = (sourceId: string | undefined, kind: CanvasConnection['kind']) => {
      if (!sourceId || sourceId === target.id || !objects[sourceId] || sources.has(sourceId)) {
        return
      }
      sources.add(sourceId)
      edges.push({ sourceId, targetId: target.id, kind })
    }
    // 已出结果的节点显示实际生成快照，草稿显示下次生成使用的引用。
    const completed = Boolean(target.src || target.assetId)
    if (!completed) {
      for (const ref of (target.nodeRun?.snapshot || target.nodeDraft)?.references || []) {
        add(ref.sourceNodeId, 'reference')
      }
    }
    if (target.lineage) {
      const params = target.lineage.params as { references?: { sourceNodeId?: string }[] }
      if (Array.isArray(params.references)) {
        for (const ref of params.references) {
          add(ref?.sourceNodeId, 'lineage')
        }
      }
      add(target.lineage.fromId, 'lineage')
    }
  }
  return edges
}

/** 端点沿用节点旋转几何；三次贝塞尔控制点沿端口方向延伸。 */
export function connectionPoints(source: Geometry, target: Geometry): number[] {
  const start = transformPoint(source, source.width, source.height / 2)
  const end = transformPoint(target, 0, target.height / 2)
  const reach = Math.max(48, Math.min(240, Math.hypot(end.x - start.x, end.y - start.y) / 2))
  const first = transformPoint(source, source.width + reach, source.height / 2)
  const second = transformPoint(target, -reach, target.height / 2)
  return [start.x, start.y, first.x, first.y, second.x, second.y, end.x, end.y]
}
