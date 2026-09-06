// 局部重绘模式(PRD §5.3-③ / 线框 S10):
// 双击图或菜单进入 → 其余对象变暗 → 笔刷涂抹蒙版(白=重绘区域,黑底白笔 PNG)→
// 区域提示词生成 → 结果为新对象(源图保留)→ 退出还原。
// 笔画以图像坐标录制,导出时按原图尺寸重放到离屏 canvas,规避缩放误差。

/** @typedef {{ points: number[], size: number, erase: boolean }} InpaintStroke */

/**
 * @param {{
 *   engine: any,
 *   getDocStore: () => any,
 *   onSubmit: (spec: { srcObj: any, maskDataUrl: string, prompt: string }) => Promise<void>,
 * }} options
 */
export function createInpaintSession(options) {
  const { engine, getDocStore, onSubmit } = options
  /** @type {string | null} 源对象 id */
  let srcId = null
  /** @type {InpaintStroke[]} */
  let strokes = []
  let brushSize = 64
  let erase = false
  /** @type {any} 蒙版笔画层(图像坐标系,跟随相机) */
  let strokeLayer = null
  /** @type {any} 蒙版当前笔迹节点 */
  let currentLine = null

  const isActive = () => srcId !== null
  const getSrcObj = () => {
    const doc = getDocStore()?.doc
    const obj = doc?.objects[srcId || '']
    return obj && obj.kind === 'image' ? obj : null
  }

  /** 屏幕坐标 → 源图局部坐标 */
  /** @param {{ x: number, y: number }} screen */
  function screenToImage(screen) {
    const obj = getSrcObj()
    return {
      x: (screen.x - obj.x) / obj.width,
      y: (screen.y - obj.y) / obj.height,
    }
  }

  function clearStrokeLayer() {
    if (strokeLayer) {
      strokeLayer.destroy()
      strokeLayer = null
      currentLine = null
    }
  }

  /** 重放笔画到蒙版层(Konva 线条,屏幕观感) */
  function redrawStrokeLayer() {
    clearStrokeLayer()
    const obj = getSrcObj()
    if (!obj) {
      return
    }
    strokeLayer = new Konva.Layer({ listening: false })
    engine.stage.add(strokeLayer)
    for (const stroke of strokes) {
      strokeLayer.add(
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
  /** @param {InpaintStroke} stroke @param {any} obj */
  function strokeToPoints(stroke, obj) {
    const pts = []
    for (let i = 0; i < stroke.points.length; i += 2) {
      pts.push(obj.x + stroke.points[i] * obj.width)
      pts.push(obj.y + stroke.points[i + 1] * obj.height)
    }
    return pts
  }

  /** 导出蒙版:黑底白笔 PNG(与源图原始像素同尺寸) */
  /** @param {any} obj */
  async function exportMask(obj) {
    // 取原图尺寸
    const img = new Image()
    img.src = obj.assetId ? `/assets/${obj.assetId}/original.${obj.ext || 'png'}` : obj.src
    await new Promise((resolve, reject) => {
      img.onload = resolve
      img.onerror = () => reject(new Error('源图读取失败'))
    })
    const canvas = document.createElement('canvas')
    canvas.width = img.naturalWidth || obj.width
    canvas.height = img.naturalHeight || obj.height
    const ctx = /** @type {CanvasRenderingContext2D} */ (canvas.getContext('2d'))
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
    return canvas.toDataURL('image/png')
  }

  function attachPointerHandlers() {
    const container = engine.stage.container()
    /** @param {MouseEvent} e @returns {{ x: number, y: number }} */
    const pos = (e) => {
      const rect = container.getBoundingClientRect()
      return engine.screenToWorld({ x: e.clientX - rect.left, y: e.clientY - rect.top })
    }
    const down = (/** @type {MouseEvent} */ e) => {
      if (!isActive() || e.button !== 0) {
        return
      }
      const obj = getSrcObj()
      const world = pos(e)
      // 只在源图范围内涂抹
      if (
        world.x < obj.x ||
        world.x > obj.x + obj.width ||
        world.y < obj.y ||
        world.y > obj.y + obj.height
      ) {
        return
      }
      const p = screenToImage(world)
      strokes.push({ points: [p.x, p.y], size: brushSize / obj.width, erase })
      currentLine = strokes[strokes.length - 1]
      redrawStrokeLayer()
      e.preventDefault()
    }
    const move = (/** @type {MouseEvent} */ e) => {
      if (!isActive() || !currentLine) {
        return
      }
      const obj = getSrcObj()
      const world = pos(e)
      const cx = Math.min(Math.max(world.x, obj.x), obj.x + obj.width)
      const cy = Math.min(Math.max(world.y, obj.y), obj.y + obj.height)
      const p = screenToImage({ x: cx, y: cy })
      currentLine.points.push(p.x, p.y)
      redrawStrokeLayer()
      e.preventDefault()
    }
    const up = () => {
      currentLine = null
    }
    container.addEventListener('mousedown', down)
    container.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
    return () => {
      container.removeEventListener('mousedown', down)
      container.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', up)
    }
  }

  /** @type {(() => void) | null} */
  let detachPointer = null
  /** @type {Map<string, number>} 进重绘前的其他对象透明度 */
  const savedOpacity = new Map()

  return {
    isActive,
    getSrcObj,
    get brush() {
      return brushSize
    },
    /** @param {number} size */
    setBrush(size) {
      brushSize = size
    },
    /** @param {boolean} on */
    setErase(on) {
      erase = on
    },
    /** @returns {boolean} 当前是否擦除模式 */
    get eraseOn() {
      return erase
    },
    clearStrokes() {
      strokes = []
      redrawStrokeLayer()
    },
    /** 进入重绘模式:其余对象压暗,启动蒙版笔刷 */
    /** @param {string} objId */
    enter(objId) {
      const doc = getDocStore()?.doc
      const obj = doc?.objects[objId]
      if (!obj || obj.kind !== 'image' || isActive()) {
        return false
      }
      srcId = objId
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
    /** 退出并还原 */
    exit() {
      if (!isActive()) {
        return
      }
      srcId = null
      strokes = []
      clearStrokeLayer()
      for (const node of engine.contentLayer.getChildren()) {
        if (savedOpacity.has(node.id())) {
          node.opacity(savedOpacity.get(node.id()))
        }
      }
      savedOpacity.clear()
      engine.contentLayer.draw()
      detachPointer?.()
      detachPointer = null
    },
    /** 提交重绘:导出蒙版并回调 onSubmit */
    /** @param {string} prompt */
    async submit(prompt) {
      const obj = getSrcObj()
      if (!obj) {
        throw new Error('不在重绘模式')
      }
      if (strokes.length === 0) {
        throw new Error('请先涂抹要重绘的区域')
      }
      const maskDataUrl = await exportMask(obj)
      await onSubmit({ srcObj: obj, maskDataUrl, prompt })
    },
  }
}
