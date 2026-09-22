import type { CanvasObj } from '../state/commands.js'
import type { Point, Rect, ResizeHandle } from './types.js'

export const MIN_OBJECT_SIZE = 24

export function normalizeRect(a: Point, b: Point): Rect {
  return {
    x: Math.min(a.x, b.x),
    y: Math.min(a.y, b.y),
    width: Math.abs(a.x - b.x),
    height: Math.abs(a.y - b.y),
  }
}

export function rectIntersects(a: Rect, b: Rect): boolean {
  return (
    a.x <= b.x + b.width && a.x + a.width >= b.x && a.y <= b.y + b.height && a.y + a.height >= b.y
  )
}

export function objectRect(obj: CanvasObj): Rect {
  return { x: obj.x, y: obj.y, width: obj.width, height: obj.height }
}

export function resizeFromHandle(
  origin: Rect,
  handle: ResizeHandle,
  delta: Point,
  minimum = MIN_OBJECT_SIZE,
): Rect {
  let left = origin.x
  let top = origin.y
  let right = origin.x + origin.width
  let bottom = origin.y + origin.height

  if (handle.includes('w')) {
    left += delta.x
  }
  if (handle.includes('e')) {
    right += delta.x
  }
  if (handle.includes('n')) {
    top += delta.y
  }
  if (handle.includes('s')) {
    bottom += delta.y
  }

  if (right - left < minimum) {
    if (handle.includes('w')) {
      left = right - minimum
    } else {
      right = left + minimum
    }
  }
  if (bottom - top < minimum) {
    if (handle.includes('n')) {
      top = bottom - minimum
    } else {
      bottom = top + minimum
    }
  }

  return { x: left, y: top, width: right - left, height: bottom - top }
}

export function angleBetween(center: Point, point: Point): number {
  return (Math.atan2(point.y - center.y, point.x - center.x) * 180) / Math.PI + 90
}
