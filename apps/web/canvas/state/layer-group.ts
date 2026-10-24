import type { CanvasObj } from './commands.js'
import { cmdBatch, cmdUpdateObject, type CanvasCommand } from './commands.js'
import type { ObjectPreview } from '../core/types.js'

/** Transient drag feedback uses the same uniform transform as the committed history command. */
export function previewLayerGroup(
  objects: Record<string, CanvasObj>,
  groupId: string,
  preview: ObjectPreview | null,
): Record<string, ObjectPreview | null> {
  const origin = objects[groupId]
  if (origin?.layerRole !== 'group') {
    return { [groupId]: preview }
  }
  const members = Object.values(objects).filter(
    (object) => object.id === groupId || object.groupId === groupId,
  )
  if (!preview) {
    return Object.fromEntries(members.map((object) => [object.id, null]))
  }
  const scales: number[] = []
  if (preview.width !== undefined) {
    scales.push(preview.width / origin.width)
  }
  if (preview.height !== undefined) {
    scales.push(preview.height / origin.height)
  }
  const scale = scales.length ? Math.min(...scales) : 1
  if (!(scale > 0) || !Number.isFinite(scale)) {
    return { [groupId]: preview }
  }
  const x = preview.x ?? origin.x
  const y = preview.y ?? origin.y
  return Object.fromEntries(
    members.map((object) => [
      object.id,
      {
        x: x + (object.x - origin.x) * scale,
        y: y + (object.y - origin.y) * scale,
        width: object.width * scale,
        height: object.height * scale,
        ...(object.fontSize ? { fontSize: Math.max(1, Math.round(object.fontSize * scale)) } : {}),
        ...(object.lineBoxes
          ? {
              lineBoxes: object.lineBoxes.map((line) => ({
                ...line,
                x: line.x * scale,
                y: line.y * scale,
                width: line.width * scale,
                height: line.height * scale,
              })),
            }
          : {}),
      },
    ]),
  )
}

/** Move and scale a poster group and every child by the same delta. Undo is the inverse batch. */
export function transformLayerGroup(
  objects: Record<string, CanvasObj>,
  groupId: string,
  delta: { dx: number; dy: number; scale: number },
): CanvasCommand {
  const origin = objects[groupId]
  const members = Object.values(objects).filter(
    (object) => object.id === groupId || object.groupId === groupId,
  )
  const commands = members.map((object) => {
    const relX = object.id === groupId || !origin ? 0 : object.x - origin.x
    const relY = object.id === groupId || !origin ? 0 : object.y - origin.y
    const baseX = origin ? origin.x : object.x
    const baseY = origin ? origin.y : object.y
    const patch = {
      x: baseX + delta.dx + relX * delta.scale,
      y: baseY + delta.dy + relY * delta.scale,
      width: object.width * delta.scale,
      height: object.height * delta.scale,
      ...(object.fontSize
        ? { fontSize: Math.max(1, Math.round(object.fontSize * delta.scale)) }
        : {}),
      ...(object.lineBoxes
        ? {
            lineBoxes: object.lineBoxes.map((line) => ({
              ...line,
              x: line.x * delta.scale,
              y: line.y * delta.scale,
              width: line.width * delta.scale,
              height: line.height * delta.scale,
            })),
          }
        : {}),
    }
    const prev = {
      x: object.x,
      y: object.y,
      width: object.width,
      height: object.height,
      ...(object.fontSize ? { fontSize: object.fontSize } : {}),
      ...(object.lineBoxes ? { lineBoxes: object.lineBoxes } : {}),
    }
    return cmdUpdateObject(object.id, patch, prev)
  })
  return cmdBatch(commands)
}
