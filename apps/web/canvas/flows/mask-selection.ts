/** 点击识别蒙版的一个连通主体，切换该主体的选区；不把背景猜成主体。 */
export function toggleMaskRegion(
  detected: { width: number; height: number; data: Uint8ClampedArray },
  current: { data: Uint8ClampedArray },
  x: number,
  y: number,
): boolean {
  const { width, height, data } = detected
  x = Math.floor(x)
  y = Math.floor(y)
  if (x < 0 || y < 0 || x >= width || y >= height) {
    return false
  }
  const start = y * width + x
  if (data[start * 4] < 128) {
    return false
  }
  const value = current.data[start * 4] >= 128 ? 0 : 255
  const seen = new Uint8Array(width * height)
  const queue = new Int32Array(width * height)
  let head = 0,
    tail = 1
  queue[0] = start
  seen[start] = 1
  while (head < tail) {
    const index = queue[head++]
    const p = index * 4
    current.data[p] = current.data[p + 1] = current.data[p + 2] = value ? data[p] : 0
    current.data[p + 3] = 255
    for (const next of [
      index % width ? index - 1 : -1,
      index % width < width - 1 ? index + 1 : -1,
      index - width,
      index + width,
    ]) {
      if (next >= 0 && next < seen.length && !seen[next] && data[next * 4] >= 128) {
        seen[next] = 1
        queue[tail++] = next
      }
    }
  }
  return true
}
