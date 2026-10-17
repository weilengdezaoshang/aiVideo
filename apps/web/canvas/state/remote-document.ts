import type { CanvasDocData } from './doc-store.js'
import type { CanvasObj } from './commands.js'

export class RemoteDocumentConflict extends Error {
  constructor(readonly field: string) {
    super('画布同时被修改，请先解决保存冲突')
  }
}
export function sameDocumentValue(left: unknown, right: unknown): boolean {
  if (left === right) {
    return true
  }
  if (left === null || right === null || typeof left !== 'object' || typeof right !== 'object') {
    return false
  }
  if (Array.isArray(left) || Array.isArray(right)) {
    return (
      Array.isArray(left) &&
      Array.isArray(right) &&
      left.length === right.length &&
      left.every((item, index) => sameDocumentValue(item, right[index]))
    )
  }
  const lhs = left as Record<string, unknown>,
    rhs = right as Record<string, unknown>
  const keys = Object.keys(lhs)
  return (
    keys.length === Object.keys(rhs).length &&
    keys.every((key) => Object.hasOwn(rhs, key) && sameDocumentValue(lhs[key], rhs[key]))
  )
}
function mergeValue(base: unknown, local: unknown, remote: unknown, field: string): unknown {
  if (sameDocumentValue(local, base)) {
    return remote
  }
  if (sameDocumentValue(remote, base) || sameDocumentValue(local, remote)) {
    return local
  }
  throw new RemoteDocumentConflict(field)
}

/** Merge independent layout and generated-media changes; never overwrite conflicting edits. */
export function mergeRemoteDocument(
  base: CanvasDocData,
  local: CanvasDocData,
  remote: CanvasDocData,
): CanvasDocData {
  if (base.id !== local.id || local.id !== remote.id) {
    throw new RemoteDocumentConflict('id')
  }
  const objects: Record<string, CanvasObj> = {}
  for (const id of new Set([
    ...Object.keys(base.objects),
    ...Object.keys(local.objects),
    ...Object.keys(remote.objects),
  ])) {
    const before = base.objects[id],
      current = local.objects[id],
      incoming = remote.objects[id]
    if (!before || !current || !incoming) {
      const node = mergeValue(before, current, incoming, `objects.${id}`) as CanvasObj | undefined
      if (node) {
        objects[id] = structuredClone(node)
      }
      continue
    }
    const merged: Record<string, unknown> = {}
    const left = before as unknown as Record<string, unknown>
    const middle = current as unknown as Record<string, unknown>
    const right = incoming as unknown as Record<string, unknown>
    for (const key of new Set([
      ...Object.keys(left),
      ...Object.keys(middle),
      ...Object.keys(right),
    ])) {
      const value = mergeValue(left[key], middle[key], right[key], `objects.${id}.${key}`)
      if (value !== undefined) {
        merged[key] = value
      }
    }
    // All required CanvasObj fields come from validated documents; the merge cannot invent a shape.
    objects[id] = merged as CanvasObj
  }
  const keep = (order: string[]) => order.filter((id) => !!objects[id])
  const baseOrder = base.order.filter((id) => !!base.objects[id])
  const relative = (order: string[]) =>
    order.filter((id) => baseOrder.includes(id) && !!objects[id])
  let order: string[]
  if (sameDocumentValue(relative(local.order), relative(baseOrder))) {
    order = keep(remote.order)
  } else if (
    sameDocumentValue(relative(remote.order), relative(baseOrder)) ||
    sameDocumentValue(relative(local.order), relative(remote.order))
  ) {
    order = keep(local.order)
  } else {
    throw new RemoteDocumentConflict('order')
  }
  for (const id of [...local.order, ...remote.order]) {
    if (objects[id] && !order.includes(id)) {
      order.push(id)
    }
  }
  const merged = { ...remote, objects, order }
  for (const key of ['name', 'groups', 'chat', 'generations', 'storyboard', 'timeline'] as const) {
    const value = mergeValue(base[key], local[key], remote[key], key)
    Object.assign(merged, { [key]: value })
  }
  return merged
}
