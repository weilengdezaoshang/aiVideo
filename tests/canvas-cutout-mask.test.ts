import assert from 'node:assert/strict'
import test from 'node:test'
import { maskLocalPoint, paintCutoutMask } from '../apps/web/canvas/flows/cutout-mask.js'
import { cutoutPlacement } from '../apps/web/canvas/flows/cutout-placement.js'
import type { CanvasObj } from '../apps/web/canvas/state/commands.js'
const source: CanvasObj = {
  id: 'image',
  kind: 'image',
  x: 100,
  y: 50,
  width: 200,
  height: 100,
  rotation: 90,
}

test('蒙版笔画逆旋转至原图坐标，不把旋转后的画笔提交到错误区域', () => {
  const point = maskLocalPoint(source, { x: 80, y: 90 })
  assert.ok(Math.abs(point.x - 40) < 0.0001)
  assert.ok(Math.abs(point.y - 20) < 0.0001)
})

test('保留和擦除按历史顺序分别写入白色和黑色，点击也生成完整笔刷圆点', () => {
  const fills: string[] = []
  const dabs: number[] = []
  const context = {
    fillStyle: '',
    strokeStyle: '',
    lineCap: '',
    lineJoin: '',
    lineWidth: 0,
    save() {},
    restore() {},
    scale() {},
    drawImage() {},
    fillRect() {},
    beginPath() {},
    moveTo() {},
    lineTo() {},
    stroke() {},
    arc(_x: number, _y: number, radius: number) {
      dabs.push(radius)
    },
    fill() {
      fills.push(this.fillStyle)
    },
  }
  // Only the Canvas 2D methods used by mask composition are stubbed at this test boundary.
  paintCutoutMask(context as unknown as CanvasRenderingContext2D, source, 1000, 500, null, [
    { mode: 'paint', width: 20, points: [{ x: 80, y: 90 }] },
    { mode: 'erase', width: 8, points: [{ x: 80, y: 90 }] },
  ])
  assert.deepEqual(fills, ['#fff', '#000'])
  assert.deepEqual(dabs, [10, 4])
})

test('工具条跟随图片，靠近底部时上移，窄视口不超出左右边界', () => {
  const image = { ...source, rotation: 0, x: 200, y: 100, width: 200, height: 200 }
  const camera = { x: 0, y: 0, scale: 1 }
  const below = cutoutPlacement(image, camera, { width: 1000, height: 800 })
  assert.equal(below.top, 316)
  const above = cutoutPlacement({ ...image, y: 550 }, camera, { width: 1000, height: 800 })
  assert.equal(above.top, 470)
  const narrow = cutoutPlacement(image, camera, { width: 390, height: 800 })
  assert.ok(narrow.left - narrow.width / 2 >= 16)
  assert.ok(narrow.left + narrow.width / 2 <= 390 - 16)
})
