import type { MaskStroke, Point } from '../core/types.js'
import type { CanvasObj } from '../state/commands.js'

/** Inverse the object's rotation before mapping a canvas stroke into image pixels. */
export function maskLocalPoint(
  source: Pick<CanvasObj, 'x' | 'y' | 'rotation'>,
  point: Point,
): Point {
  const angle = -((source.rotation ?? 0) * Math.PI) / 180
  const x = point.x - source.x
  const y = point.y - source.y
  return {
    x: x * Math.cos(angle) - y * Math.sin(angle),
    y: x * Math.sin(angle) + y * Math.cos(angle),
  }
}

/** Shared by the live selection and the submitted grayscale PNG. */
export function paintCutoutMask(
  context: CanvasRenderingContext2D,
  source: CanvasObj,
  width: number,
  height: number,
  base: CanvasImageSource | null,
  edits: readonly MaskStroke[],
): void {
  context.save()
  context.fillStyle = '#000'
  context.fillRect(0, 0, width, height)
  if (base) {
    context.drawImage(base, 0, 0, width, height)
  }
  context.scale(width / source.width, height / source.height)
  context.lineCap = 'round'
  context.lineJoin = 'round'
  for (const edit of edits) {
    if (!edit.points.length) {
      continue
    }
    const points = edit.points.map((point) => maskLocalPoint(source, point))
    context.strokeStyle = context.fillStyle = edit.mode === 'erase' ? '#000' : '#fff'
    context.lineWidth = edit.width
    context.beginPath()
    context.moveTo(points[0].x, points[0].y)
    for (const point of points.slice(1)) {
      context.lineTo(point.x, point.y)
    }
    context.stroke()
    // A click paints a full brush dab, including engines that omit zero-length lines.
    context.beginPath()
    context.arc(points[0].x, points[0].y, edit.width / 2, 0, Math.PI * 2)
    context.fill()
  }
  context.restore()
}
