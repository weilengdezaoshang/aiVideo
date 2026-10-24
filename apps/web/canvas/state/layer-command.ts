import type { CanvasCommand, CommandDoc } from './commands.js'
import { cmdBatch, cmdMoveObjects, cmdRemoveObjects, cmdUpdateObject } from './commands.js'
import { transformLayerGroup } from './layer-group.js'

const GEOMETRY = new Set(['x', 'y', 'width', 'height'])

function explicitGeometry(command: CanvasCommand): Set<string> {
  if (command.type === 'batch') {
    return new Set(command.commands.flatMap((child) => [...explicitGeometry(child)]))
  }
  if (command.type === 'moveObjects') {
    return new Set(command.moves.map((move) => move.id))
  }
  if (
    command.type === 'updateObject' &&
    Object.keys(command.patch).some((key) => GEOMETRY.has(key))
  ) {
    return new Set([command.id])
  }
  return new Set()
}

/** Expand once at history intake, so undo and redo replay complete immutable snapshots. */
export function expandLayerCommand(doc: CommandDoc, command: CanvasCommand): CanvasCommand {
  const explicit = explicitGeometry(command)
  function expand(current: CanvasCommand): CanvasCommand {
    if (current.type === 'batch') {
      return cmdBatch(current.commands.map(expand))
    }
    if (current.type === 'moveObjects') {
      const moves = new Map(current.moves.map((move) => [move.id, move]))
      for (const move of current.moves) {
        const origin = doc.objects[move.id]
        if (origin?.layerRole !== 'group') {
          continue
        }
        for (const child of Object.values(doc.objects)) {
          if (child.id === origin.id || child.groupId !== origin.id || explicit.has(child.id)) {
            continue
          }
          moves.set(child.id, {
            id: child.id,
            from: { x: child.x, y: child.y },
            to: { x: child.x + move.to.x - origin.x, y: child.y + move.to.y - origin.y },
          })
        }
      }
      return cmdMoveObjects([...moves.values()])
    }
    if (current.type === 'removeObjects') {
      const entries = new Map(current.entries.map((entry) => [entry.object.id, entry]))
      for (const entry of current.entries) {
        if (entry.object.layerRole !== 'group') {
          continue
        }
        for (const child of Object.values(doc.objects)) {
          if (child.groupId === entry.object.id && !entries.has(child.id)) {
            entries.set(child.id, {
              object: structuredClone(child),
              orderIndex: doc.order.indexOf(child.id),
            })
          }
        }
      }
      return cmdRemoveObjects([...entries.values()])
    }
    if (current.type !== 'updateObject') {
      return current
    }
    const origin = doc.objects[current.id]
    if (
      origin?.layerRole !== 'group' ||
      !Object.keys(current.patch).some((key) => GEOMETRY.has(key))
    ) {
      return current
    }
    const number = (key: 'x' | 'y' | 'width' | 'height') => {
      const value = current.patch[key]
      return typeof value === 'number' && Number.isFinite(value) ? value : origin[key]
    }
    const scales: number[] = []
    if (typeof current.patch.width === 'number') {
      scales.push(number('width') / origin.width)
    }
    if (typeof current.patch.height === 'number') {
      scales.push(number('height') / origin.height)
    }
    const scale = scales.length ? Math.min(...scales) : 1
    if (!(scale > 0) || !Number.isFinite(scale)) {
      throw new Error('invalid_layer_group_scale')
    }
    const transformed = transformLayerGroup(doc.objects, origin.id, {
      dx: number('x') - origin.x,
      dy: number('y') - origin.y,
      scale,
    })
    const commands = transformed.type === 'batch' ? transformed.commands : [transformed]
    const filtered = commands.filter(
      (child) => child.type !== 'updateObject' || child.id === origin.id || !explicit.has(child.id),
    )
    const extra = Object.fromEntries(
      Object.entries(current.patch).filter(([key]) => !GEOMETRY.has(key)),
    )
    if (Object.keys(extra).length) {
      filtered.push(
        cmdUpdateObject(
          origin.id,
          extra,
          Object.fromEntries(Object.keys(extra).map((key) => [key, current.prevPatch[key]])),
        ),
      )
    }
    return cmdBatch(filtered)
  }
  return expand(command)
}
