// 变换以对象左上角为坐标原点;旋转角度以度保存。
export type Geometry = { x: number; y: number; width: number; height: number; rotation?: number }

export function transformPoint(box: Geometry, x: number, y: number): { x: number; y: number } {
  const angle = ((box.rotation || 0) * Math.PI) / 180
  return {
    x: box.x + x * Math.cos(angle) - y * Math.sin(angle),
    y: box.y + x * Math.sin(angle) + y * Math.cos(angle),
  }
}

export function transformBounds(box: Geometry): Geometry {
  const points = [
    [0, 0],
    [box.width, 0],
    [0, box.height],
    [box.width, box.height],
  ].map(([x, y]) => transformPoint(box, x, y))
  const x = Math.min(...points.map((p) => p.x))
  const y = Math.min(...points.map((p) => p.y))
  return {
    x,
    y,
    width: Math.max(...points.map((p) => p.x)) - x,
    height: Math.max(...points.map((p) => p.y)) - y,
  }
}

/** 固定对侧控制点;角点可等比缩放,边中点(0.5)只改变对应宽或高。 */
export function resizeFromCorner(
  box: Geometry,
  pointer: { x: number; y: number },
  cx: number,
  cy: number,
  keepRatio: boolean,
): Geometry {
  const fixed = transformPoint(box, (1 - cx) * box.width, (1 - cy) * box.height)
  const angle = ((box.rotation || 0) * Math.PI) / 180
  const dx = pointer.x - fixed.x,
    dy = pointer.y - fixed.y
  const localX = dx * Math.cos(angle) + dy * Math.sin(angle)
  const localY = -dx * Math.sin(angle) + dy * Math.cos(angle)
  let width = cx === 0.5 ? box.width : Math.max(48, localX * (cx * 2 - 1))
  let height = cy === 0.5 ? box.height : Math.max(48, localY * (cy * 2 - 1))
  if (keepRatio) {
    const vx = box.width * (cx * 2 - 1),
      vy = box.height * (cy * 2 - 1)
    const ratio = Math.min(
      8,
      Math.max(
        48 / Math.min(box.width, box.height),
        (localX * vx + localY * vy) / (vx * vx + vy * vy),
      ),
    )
    width = box.width * ratio
    height = box.height * ratio
  }
  const origin = transformPoint(
    { ...box, x: fixed.x, y: fixed.y },
    -(1 - cx) * width,
    -(1 - cy) * height,
  )
  return { ...origin, width, height, rotation: box.rotation || 0 }
}

export function rotateAroundCenter(box: Geometry, rotation: number): Geometry {
  const center = transformPoint(box, box.width / 2, box.height / 2)
  const origin = transformPoint({ ...box, ...center, rotation }, -box.width / 2, -box.height / 2)
  return { ...origin, width: box.width, height: box.height, rotation }
}

/** 双向缩放光标跟随对象的本地轴旋转。 */
export function resizeCursor(cx: number, cy: number, rotation: number): string {
  const angle = (Math.atan2(cy - 0.5, cx - 0.5) * 180) / Math.PI + rotation
  const direction = Math.round((((angle % 180) + 180) % 180) / 45) % 4
  return ['ew-resize', 'nwse-resize', 'ns-resize', 'nesw-resize'][direction]
}
