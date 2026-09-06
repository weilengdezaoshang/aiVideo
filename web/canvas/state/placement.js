// 落位纯函数(可 node:test):新对象落在锚点旁第一个空位(螺旋探测,PRD §8.2/§5.3-②)。
// 网格对齐:步长 = 对象宽 + GAP;探测顺序 = 外扩正方形环,首个无重叠的位置胜出。

/**
 * @typedef {{ x: number, y: number, width: number, height: number }} Box
 */

/**
 * 判断候选位置与现有对象是否重叠(留 GAP 间距)。
 * @param {Record<string, Box>} boxes
 * @param {Box} candidate
 * @param {number} gap
 */
export function overlapsAny(boxes, candidate, gap) {
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
 * @param {{ objects: Record<string, Box> }} doc
 * @param {{ x: number, y: number }} anchor 新对象参照点(源图右上 / 视口中心)
 * @param {{ width: number, height: number }} size
 * @param {{ gap?: number, maxRings?: number }} [opts]
 * @returns {{ x: number, y: number }}
 */
export function findFreePosition(doc, anchor, size, opts = {}) {
  const gap = opts.gap ?? 40
  const maxRings = opts.maxRings ?? 24
  const stepX = size.width + gap
  const stepY = size.height + gap

  /** 环 k 的候选位(右 → 下 → 左 → 上),k=0 即锚点本身 */
  /** @param {number} k */
  function ringPositions(k) {
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
