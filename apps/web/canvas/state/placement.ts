// 落位纯函数(可 node:test):新对象落在锚点旁第一个空位(螺旋探测,PRD §8.2/§5.3-②)。
// 网格对齐:步长 = 对象宽 + GAP;探测顺序 = 外扩正方形环,首个无重叠的位置胜出。

export type Box = { x: number; y: number; width: number; height: number }

/**
 * 判断候选位置与现有对象是否重叠(留 GAP 间距)。
 */
export function overlapsAny(boxes: Record<string, Box>, candidate: Box, gap: number): boolean {
  for (const b of Object.values(boxes)) {
    if (
      candidate.x - gap < b.x + b.width &&
      candidate.x + candidate.width + gap > b.x &&
      candidate.y - gap < b.y + b.height &&
      candidate.y + candidate.height + gap > b.y
    ) {
      return true
    }
  }
  return false
}

/**
 * 螺旋探测:从锚点出发,按环向外扩找第一个无重叠的网格位。
 * @param anchor 新对象参照点(源图右上 / 视口中心)
 */
export function findFreePosition(
  doc: { objects: Record<string, Box> },
  anchor: { x: number; y: number },
  size: { width: number; height: number },
  opts: { gap?: number; maxRings?: number } = {},
): { x: number; y: number } {
  const gap = opts.gap ?? 40
  const maxRings = opts.maxRings ?? 24
  const stepX = size.width + gap
  const stepY = size.height + gap

  /** 环 k 的候选位(右 → 下 → 左 → 上),k=0 即锚点本身 */
  function ringPositions(k: number): { x: number; y: number }[] {
    if (k === 0) {
      return [{ x: anchor.x, y: anchor.y }]
    }
    const positions = []
    // 顶边:从右上角向左
    for (let i = 0; i <= k; i++) {
      positions.push({ x: anchor.x + (k - i) * stepX, y: anchor.y - k * stepY })
    }
    // 右边:顶右角下方
    for (let i = 1; i <= k; i++) {
      positions.push({ x: anchor.x + k * stepX, y: anchor.y + (i - 1) * stepY })
    }
    // 底边:右下角向左
    for (let i = 1; i <= k; i++) {
      positions.push({ x: anchor.x + (k - i) * stepX, y: anchor.y + k * stepY })
    }
    // 左边:底部向上
    for (let i = 1; i < k; i++) {
      positions.push({ x: anchor.x - k * stepX, y: anchor.y + (k - i) * stepY })
    }
    return positions
  }

  for (let k = 0; k <= maxRings; k++) {
    for (const pos of ringPositions(k)) {
      const candidate = { ...pos, width: size.width, height: size.height }
      if (!overlapsAny(doc.objects, candidate, gap)) {
        return { x: pos.x, y: pos.y }
      }
    }
  }
  // 探测失败:锚点直接落位(允许重叠,极端画布下的兜底)
  return { x: anchor.x, y: anchor.y }
}

/**
 * 把候选位置钳进视野矩形(世界坐标,四周留 margin),保证新建对象落在用户视野内。
 * findFreePosition 的螺旋探测可能走到视野外(首环右上即 y 为负),
 * 钳制后若与既有对象重叠则允许重叠:可见性优先于无重叠。
 * 视野小于对象时退化为视野左上角 margin 处,保证对象左上角可见。
 */
export function clampIntoRect(
  pos: { x: number; y: number },
  size: { width: number; height: number },
  view: { x: number; y: number; width: number; height: number },
  margin = 24,
): { x: number; y: number } {
  const minX = view.x + margin
  const minY = view.y + margin
  const maxX = Math.max(minX, view.x + view.width - margin - size.width)
  const maxY = Math.max(minY, view.y + view.height - margin - size.height)
  return {
    x: Math.min(Math.max(pos.x, minX), maxX),
    y: Math.min(Math.max(pos.y, minY), maxY),
  }
}

/** 在源对象右侧生成一行等间距锚点。 */
export function variantRowAnchors(
  source: Box,
  count: number,
  gap = 40,
): { x: number; y: number }[] {
  return Array.from({ length: count }, (_, index) => ({
    x: source.x + source.width + gap + index * (source.width + gap),
    y: source.y,
  }))
}

export type InsertionPlan = {
  x: number
  y: number
  moves: { id: string; from: { x: number; y: number }; to: { x: number; y: number } }[]
}

/**
 * 派生结果(抠图)插入源图右侧,组内后续成员顺延(PRD §6):
 *   - 源图属于某组 → 插入点 = 源图右邻(间距 24 世界单位),
 *     同组中位于源图右侧的成员整体右移,腾出一个槽位;
 *   - 组外对象与源图本身都不移动;独立图片直接右邻放置。
 */
export function groupInsertion(
  doc: { objects: Record<string, Box & { id?: string; groupId?: string }> },
  sourceId: string,
  insertSize: { width: number; height: number },
  opts: { gap?: number } = {},
): InsertionPlan {
  const gap = opts.gap ?? 24
  const source = doc.objects[sourceId]
  if (!source) {
    return { x: 0, y: 0, moves: [] }
  }
  const insertX = source.x + source.width + gap
  const moves: InsertionPlan['moves'] = []
  if (source.groupId) {
    const shift = insertSize.width + gap
    for (const obj of Object.values(doc.objects)) {
      if (
        obj.id &&
        obj.id !== sourceId &&
        obj.groupId === source.groupId &&
        obj.x >= source.x + source.width
      ) {
        moves.push({ id: obj.id, from: { x: obj.x, y: obj.y }, to: { x: obj.x + shift, y: obj.y } })
      }
    }
  }
  return { x: insertX, y: source.y, moves }
}
