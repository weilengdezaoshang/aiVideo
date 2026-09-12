// 局部重绘模式(PRD §5.3-③ / 线框 S10):
// 双击图或菜单进入 → 其余对象变暗 → 笔刷涂抹蒙版(白=重绘区域,黑底白笔 PNG)→
// 区域提示词生成 → 结果为新对象(源图保留)→ 退出还原。
// 笔画以图像坐标录制,导出时按原图尺寸重放到离屏 canvas,规避缩放误差。

import type { CanvasEngine } from '../engine/stage.js'
import type { DocStore } from '../state/doc-store.js'
import type { CanvasObj } from '../state/commands.js'

export type InpaintStroke = { points: number[]; size: number; erase: boolean }

export type InpaintSubmitSpec = {
  srcObj: CanvasObj
  mask: { dataUrl: string; width: number; height: number }
  prompt: string
}

export type InpaintSession = {
  isActive(): boolean
  getSrcObj(): CanvasObj | null
  readonly brush: number
  setBrush(size: number): void
  setErase(on: boolean): void
  readonly eraseOn: boolean
  clearStrokes(): void
  /** 进入重绘模式:其余对象压暗,启动蒙版笔刷 */
  enter(objId: string): boolean
  /** 退出并还原 */
  exit(): void
  /** 提交重绘:导出蒙版并回调 onSubmit */
  submit(prompt: string): Promise<void>
}

export function createInpaintSession(options: {
  engine: CanvasEngine
  getDocStore: () => DocStore | null
  onSubmit: (spec: InpaintSubmitSpec) => Promise<void>
}): InpaintSession {
  const { engine, getDocStore, onSubmit } = options
  /** 源对象 id */
  let srcId: string | null = null
  let strokes: InpaintStroke[] = []
  let redoStrokes: InpaintStroke[] = []
  let brushSize = 64
  let erase = false
  /** 蒙版笔画层(图像坐标系,跟随相机) */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Konva 以 UMD vendor 引入,无类型来源
  let strokeLayer: any = null
  /** 蒙版当前笔迹节点 */
  let currentLine: InpaintStroke | null = null

  const isActive = () => srcId !== null
  const getSrcObj = () => {
    const doc = getDocStore()?.doc
    const obj = doc?.objects[srcId || '']
    return obj && obj.kind === 'image' ? obj : null
  }

  /** 屏幕坐标 → 源图局部坐标 */
  function screenToImage(screen: { x: number; y: number }) {
    const obj = getSrcObj()!
    const angle = ((obj.rotation || 0) * Math.PI) / 180
    const dx = screen.x - obj.x,
      dy = screen.y - obj.y
    return {
      x: (dx * Math.cos(angle) + dy * Math.sin(angle)) / obj.width,
      y: (-dx * Math.sin(angle) + dy * Math.cos(angle)) / obj.height,
    }
  }

  function clearStrokeLayer() {
    if (strokeLayer) {
      engine.unregisterCamLayer(strokeLayer)
      strokeLayer.destroy()
      strokeLayer = null
    }
  }

  /** 重放笔画到蒙版层(Konva 线条,屏幕观感) */
  function redrawStrokeLayer() {
    const active = currentLine
    clearStrokeLayer()
    currentLine = active
    const obj = getSrcObj()
    if (!obj) {
      return
    }
    strokeLayer = new Konva.Layer({ listening: false })
    engine.stage.add(strokeLayer)
    const imageGroup = new Konva.Group({
      x: obj.x,
      y: obj.y,
      rotation: obj.rotation || 0,
      clipX: 0,
      clipY: 0,
      clipWidth: obj.width,
      clipHeight: obj.height,
    })
    strokeLayer.add(imageGroup)
    for (const stroke of strokes) {
      imageGroup.add(
        new Konva.Line({
          points: strokeToPoints(stroke, obj),
          stroke: stroke.erase ? 'rgba(0,0,0,0.55)' : 'rgba(255,80,80,0.62)',
          strokeWidth: stroke.size * obj.width,
          lineCap: 'round',
          lineJoin: 'round',
          tension: 0,
        }),
      )
    }
    engine.registerCamLayer(strokeLayer)
    strokeLayer.draw()
  }

  /** 图像坐标笔画 → 屏幕坐标点集(Konva Line 需要) */
  function strokeToPoints(stroke: InpaintStroke, obj: CanvasObj) {
    const pts: number[] = []
    for (let i = 0; i < stroke.points.length; i += 2) {
      pts.push(stroke.points[i] * obj.width)
      pts.push(stroke.points[i + 1] * obj.height)
    }
    return pts
  }

  /**
   * 导出蒙版:黑底白笔 PNG(与源图原始像素同尺寸);返回自然尺寸供生成参数对齐。
   */
  async function exportMask(obj: CanvasObj) {
    const img = new Image()
    img.src = obj.assetId ? `/assets/${obj.assetId}/original.${obj.ext || 'png'}` : obj.src!
    await new Promise((resolve, reject) => {
      img.onload = resolve
      img.onerror = () => reject(new Error('源图读取失败'))
    })
    const canvas = document.createElement('canvas')
    canvas.width = img.naturalWidth || obj.width
    canvas.height = img.naturalHeight || obj.height
    const ctx = canvas.getContext('2d') as CanvasRenderingContext2D
    ctx.fillStyle = '#000'
    ctx.fillRect(0, 0, canvas.width, canvas.height)
    ctx.lineCap = 'round'
    ctx.lineJoin = 'round'
    const sx = canvas.width / obj.width
    const sy = canvas.height / obj.height
    for (const stroke of strokes) {
      ctx.globalCompositeOperation = stroke.erase ? 'destination-out' : 'source-over'
      ctx.strokeStyle = stroke.erase ? 'rgba(0,0,0,1)' : '#fff'
      ctx.lineWidth = stroke.size * obj.width * sx
      ctx.beginPath()
      const pts = strokeToPoints(stroke, obj)
      ctx.moveTo(pts[0] * sx, pts[1] * sy)
      for (let i = 2; i < pts.length; i += 2) {
        ctx.lineTo(pts[i] * sx, pts[i + 1] * sy)
      }
      if (pts.length === 2) {
        // 单点 = 圆点
        ctx.lineTo(pts[0] * sx + 0.01, pts[1] * sy)
      }
      ctx.stroke()
    }
    ctx.globalCompositeOperation = 'source-over'
    return { dataUrl: canvas.toDataURL('image/png'), width: canvas.width, height: canvas.height }
  }

  function attachPointerHandlers() {
    const container = engine.stage.container()
    const pos = (e: MouseEvent) => {
      const rect = container.getBoundingClientRect()
      return engine.screenToWorld({ x: e.clientX - rect.left, y: e.clientY - rect.top })
    }
    const down = (e: MouseEvent) => {
      if (!isActive() || e.button !== 0) {
        return
      }
      const p = screenToImage(pos(e))
      const obj = getSrcObj()!
      if (p.x < 0 || p.y < 0 || p.x > 1 || p.y > 1) {
        return
      }
      redoStrokes = []
      strokes.push({ points: [p.x, p.y], size: brushSize / obj.width, erase })
      currentLine = strokes[strokes.length - 1]
      redrawStrokeLayer()
      e.preventDefault()
    }
    const move = (e: MouseEvent) => {
      if (!isActive() || !currentLine) {
        return
      }
      const p = screenToImage(pos(e))
      p.x = Math.max(0, Math.min(1, p.x))
      p.y = Math.max(0, Math.min(1, p.y))
      currentLine.points.push(p.x, p.y)
      redrawStrokeLayer()
      e.preventDefault()
    }
    const up = () => {
      currentLine = null
    }
    engine.input.setMaskHandlers({
      down,
      move,
      up,
      cancel: () => {
        if (currentLine) {
          strokes.pop()
          currentLine = null
          redrawStrokeLayer()
        }
      },
      key: (event: KeyboardEvent) => {
        if (event.key === 'Escape') {
          api.exit()
          document.dispatchEvent(new CustomEvent('gencanvas:inpaint-exit'))
        } else if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'z') {
          if (event.shiftKey) {
            const stroke = redoStrokes.pop()
            if (stroke) {
              strokes.push(stroke)
            }
          } else {
            const stroke = strokes.pop()
            if (stroke) {
              redoStrokes.push(stroke)
            }
          }
          redrawStrokeLayer()
        }
      },
    })
    return () => engine.input.setMaskHandlers(null)
  }

  let detachPointer: (() => void) | null = null
  /** 进重绘前的其他对象透明度 */
  const savedOpacity = new Map<string, number>()

  const api: InpaintSession = {
    isActive,
    getSrcObj,
    get brush() {
      return brushSize
    },
    setBrush(size: number) {
      brushSize = size
    },
    setErase(on: boolean) {
      erase = on
    },
    /** 当前是否擦除模式 */
    get eraseOn() {
      return erase
    },
    clearStrokes() {
      redoStrokes = []
      strokes = []
      redrawStrokeLayer()
    },
    enter(objId: string) {
      const doc = getDocStore()?.doc
      const obj = doc?.objects[objId]
      if (
        !obj ||
        obj.kind !== 'image' ||
        isActive() ||
        !['select', 'pan'].includes(engine.interaction.getState().mode)
      ) {
        return false
      }
      engine.interaction.setMode('mask', objId)
      srcId = objId
      redoStrokes = []
      strokes = []
      erase = false
      // 其余对象变暗(线框 S10)
      savedOpacity.clear()
      for (const node of engine.contentLayer.getChildren()) {
        if (node.id() !== objId) {
          savedOpacity.set(node.id(), node.opacity())
          node.opacity(0.25)
        }
      }
      engine.contentLayer.draw()
      detachPointer = attachPointerHandlers()
      return true
    },
    exit() {
      if (!isActive()) {
        return
      }
      engine.interaction.setMode('select')
      srcId = null
      redoStrokes = []
      strokes = []
      clearStrokeLayer()
      for (const node of engine.contentLayer.getChildren()) {
        if (savedOpacity.has(node.id())) {
          node.opacity(savedOpacity.get(node.id())!)
        }
      }
      savedOpacity.clear()
      engine.contentLayer.draw()
      detachPointer?.()
      detachPointer = null
    },
    async submit(prompt: string) {
      const obj = getSrcObj()
      if (!obj) {
        throw new Error('不在重绘模式')
      }
      if (strokes.length === 0) {
        throw new Error('请先涂抹要重绘的区域')
      }
      const mask = await exportMask(obj)
      await onSubmit({ srcObj: obj, mask, prompt })
    },
  }
  return api
}
