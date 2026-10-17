import type { CameraState } from '../core/types.js'
import type { CanvasObj } from '../state/commands.js'

export function cutoutPlacement(
  source: CanvasObj | undefined,
  camera: CameraState,
  viewport: { width: number; height: number },
) {
  const width = Math.min(500, Math.max(0, viewport.width - 32))
  const angle = ((source?.rotation ?? 0) * Math.PI) / 180
  const corners = source
    ? [
        [0, 0],
        [source.width, 0],
        [0, source.height],
        [source.width, source.height],
      ].map(([x, y]) => ({
        x: (source.x + x * Math.cos(angle) - y * Math.sin(angle) - camera.x) * camera.scale,
        y: (source.y + x * Math.sin(angle) + y * Math.cos(angle) - camera.y) * camera.scale,
      }))
    : []
  const minY = Math.min(...corners.map((p) => p.y))
  const maxY = Math.max(...corners.map((p) => p.y))
  const centerX = corners.length
    ? (Math.min(...corners.map((p) => p.x)) + Math.max(...corners.map((p) => p.x))) / 2
    : viewport.width / 2
  const top =
    maxY + 16 + 64 <= viewport.height
      ? maxY + 16
      : minY - 80 >= 72
        ? minY - 80
        : viewport.height - 84
  return {
    width,
    left: Math.max(width / 2 + 16, Math.min(viewport.width - width / 2 - 16, centerX)),
    top: Math.max(
      72,
      Math.min(viewport.height - 84, Number.isFinite(top) ? top : viewport.height - 84),
    ),
  }
}
