import type { CanvasObj } from './state/commands.js'
import type { CameraState } from './core/types.js'

export type SelectionSurface =
  | 'none'
  | 'multi'
  | 'image'
  | 'video'
  | 'image-empty'
  | 'video-empty'
  | 'text'
  | 'timeline'
  | 'board'
  | 'busy'
  | 'error'
export function selectionSurface(objects: readonly CanvasObj[]): SelectionSurface {
  if (!objects.length) {
    return 'none'
  }
  if (objects.length > 1) {
    return 'multi'
  }
  const object = objects[0]
  if (object.placeholderType === 'timeline') {
    return 'timeline'
  }
  if (object.kind === 'error' || object.nodeRun?.status === 'failed') {
    return 'error'
  }
  if (
    object.gen ||
    (object.kind === 'placeholder' && object.generationRef) ||
    object.pendingConfirm ||
    object.nodeRun
  ) {
    return 'busy'
  }
  if (
    object.kind === 'image' ||
    object.kind === 'video' ||
    object.kind === 'text' ||
    object.kind === 'board'
  ) {
    return object.kind
  }
  if (object.placeholderType === 'video' || object.mediaType === 'video') {
    return 'video-empty'
  }
  return 'image-empty'
}
export function mediaSurface(surface: SelectionSurface): boolean {
  return ['image', 'video', 'image-empty', 'video-empty'].includes(surface)
}
/** Rotated corners are projected before anchoring DOM surfaces. */
export function selectionAnchor(
  objects: readonly CanvasObj[],
  camera: CameraState,
  viewport: { width: number; height: number },
) {
  const corners = objects.flatMap((object) => {
    const angle = ((object.rotation ?? 0) * Math.PI) / 180
    return [
      [0, 0],
      [object.width, 0],
      [0, object.height],
      [object.width, object.height],
    ].map(([x, y]) => ({
      x: (object.x + x * Math.cos(angle) - y * Math.sin(angle) - camera.x) * camera.scale,
      y: (object.y + x * Math.sin(angle) + y * Math.cos(angle) - camera.y) * camera.scale,
    }))
  })
  if (!corners.length) {
    return { x: viewport.width / 2, top: 70, bottom: viewport.height - 20 }
  }
  const minY = Math.min(...corners.map((p) => p.y))
  const maxY = Math.max(...corners.map((p) => p.y))
  const below = minY - 58 < 72
  return {
    x: Math.max(
      24,
      Math.min(
        viewport.width - 24,
        (Math.min(...corners.map((p) => p.x)) + Math.max(...corners.map((p) => p.x))) / 2,
      ),
    ),
    top: Math.max(72, Math.min(viewport.height - 64, below ? maxY + 18 : minY - 58)),
    bottom: Math.max(70, Math.min(viewport.height - 220, maxY + 18 + (below ? 58 : 0))),
  }
}
