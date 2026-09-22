// 压测脚手架(F1 验收:500 对象 + 50 张 2048px 图 ≥55fps)。
// 用法:画布页加查询参数 ?stress=500 —— 合成文档不入库、不自动保存,
// 程序化生成位图(JPEG data URL)填充画布,左下角显示实时 FPS。
// 仅开发/验收用,生产路径不加载(app.ts 按 ?stress 动态 import)。

import { cmdAddObjects, type CanvasObj } from './state/commands.js'
import type { DocStore } from './state/doc-store.js'

/** 程序化生成一张 w×h JPEG data URL(渐变 + 噪点 + 标注,模拟真实内容复杂度) */
function makeImage(w: number, h: number, seed: number): string {
  const canvas = document.createElement('canvas')
  canvas.width = w
  canvas.height = h
  const ctx = canvas.getContext('2d') as CanvasRenderingContext2D
  const hue = (seed * 47) % 360
  const grad = ctx.createLinearGradient(0, 0, w, h)
  grad.addColorStop(0, `hsl(${hue} 70% 55%)`)
  grad.addColorStop(1, `hsl(${(hue + 80) % 360} 70% 32%)`)
  ctx.fillStyle = grad
  ctx.fillRect(0, 0, w, h)
  const spots = Math.round((w * h) / 8192)
  for (let i = 0; i < spots; i++) {
    ctx.fillStyle = `hsla(${(hue + i * 13) % 360} 80% 60% / 0.25)`
    ctx.fillRect(Math.random() * w, Math.random() * h, w / 32, h / 32)
  }
  ctx.fillStyle = 'rgba(255,255,255,0.88)'
  ctx.font = `bold ${Math.round(h / 14)}px system-ui, sans-serif`
  ctx.textAlign = 'center'
  ctx.textBaseline = 'middle'
  ctx.fillText(`${w}×${h} #${seed}`, w / 2, h / 2)
  return canvas.toDataURL('image/jpeg', 0.72)
}

/** FPS 计数:固定定位左下角,rAF 帧率即平移缩放手感的主要代理指标 */
function startFpsMeter(totalCount: number) {
  const el = document.createElement('div')
  el.className = 'stress-fps'
  el.textContent = `压测准备中… 目标对象 ${totalCount}`
  document.body.append(el)
  let frames = 0
  let last = performance.now()
  function loop(now: number) {
    frames += 1
    if (now - last >= 500) {
      const fps = Math.round((frames * 1000) / (now - last))
      el.textContent = `压测:${totalCount} 对象 · FPS ${fps}(达标线 55)`
      el.classList.toggle('bad', fps < 55)
      frames = 0
      last = now
    }
    requestAnimationFrame(loop)
  }
  requestAnimationFrame(loop)
}

/**
 * 填充压测对象并启动 FPS 计。分批 apply + 让出主线程,避免生成大图期间页面假死。
 */
export async function installStress(count: number, { store }: { store: DocStore }) {
  const BIG = 50 // 2048px 源图数量(PRD F1 场景)
  const PLACEHOLDER = 4
  const ERROR = 2
  const VIDEO = 2
  const objects: CanvasObj[] = []
  let big = 0
  let small = 0

  // 大图:显示 512px,源 2048px;小图:显示 128px,源 256px;统一网格铺开
  for (let i = 0; i < count; i++) {
    const id = `stress-${i}`
    if (i < BIG) {
      const col = i % 8
      const row = Math.floor(i / 8)
      objects.push({
        id,
        kind: 'image',
        x: col * 576,
        y: row * 576,
        width: 512,
        height: 512,
        src: makeImage(2048, 2048, i),
      })
      big += 1
    } else if (i < BIG + PLACEHOLDER) {
      objects.push({
        id,
        kind: 'placeholder',
        x: 4800 + (i - BIG) * 160,
        y: 0,
        width: 128,
        height: 128,
        gen: { jobId: `stress-job-${i}` },
      })
    } else if (i < BIG + PLACEHOLDER + ERROR) {
      objects.push({
        id,
        kind: 'error',
        x: 4800 + (i - BIG - PLACEHOLDER) * 160,
        y: 160,
        width: 150,
        height: 96,
        errorDetail: '后端超时(压测样例)',
      })
    } else if (i < BIG + PLACEHOLDER + ERROR + VIDEO) {
      objects.push({
        id,
        kind: 'video',
        x: 4800 + (i - BIG - PLACEHOLDER - ERROR) * 160,
        y: 288,
        width: 160,
        height: 90,
      })
    } else {
      const j = i - BIG - PLACEHOLDER - ERROR - VIDEO
      const col = j % 30
      const row = Math.floor(j / 30)
      objects.push({
        id,
        kind: 'image',
        x: col * 152,
        y: 576 + row * 152,
        width: 128,
        height: 128,
        src: makeImage(256, 256, 1000 + i),
      })
      small += 1
    }
  }

  startFpsMeter(objects.length)
  const CHUNK = 20
  for (let i = 0; i < objects.length; i += CHUNK) {
    store.apply(cmdAddObjects(objects.slice(i, i + CHUNK)))
    // 2048px data URL 编码是重活:每批让出主线程渲染已入场的对象
    await new Promise((resolve) => setTimeout(resolve, 0))
  }
  console.info(
    `[stress] 已装载 ${objects.length} 对象(2048px 图 ${big}、小图 ${small}),开始平移/缩放观察 FPS`,
  )
}
