// 画布引擎:Konva 舞台 + 相机(平移/缩放)+ 网点背景。
// 相机属 TransientStore 语义(PRD §7.2):不进 undo、不入文档,仅内存 + 订阅。
// 手势(PRD §5.2):
//   平移 —— 触控板双指 / 空格+拖 / 鼠标中键
//   缩放 —— 滚轮 / 捏合(ctrl+wheel),始终以光标为锚

const MIN_SCALE = 0.05
const MAX_SCALE = 8
const WHEEL_ZOOM_STEP = 1.1
/** 网点基础点距(px,屏幕坐标);缩放跨越阈值时倍增/减半,避免极端缩放下过密过疏 */
const DOT_BASE = 18
const ZOOM_LEVELS = [0.05, 0.125, 0.25, 0.5, 1, 2, 4, 8]

/**
 * @param {HTMLElement} rootEl 画布容器(.canvas-root,网点背景作用其上)
 * @param {HTMLElement} surfaceEl Konva 舞台宿主;必须是空元素(Stage 构造会清空其内容)
 * @param {{ onCamMove?: (cam: Cam) => void }} [options]
 * @returns {CanvasEngine}
 */
export function createCanvasEngine(rootEl, surfaceEl, options = {}) {
  const stage = new Konva.Stage({
    container: surfaceEl,
    width: surfaceEl.clientWidth,
    height: surfaceEl.clientHeight,
  })
  /** 对象层:T6 起渲染文档对象;相机直接作用于此层 */
  const contentLayer = new Konva.Layer({ listening: true })
  stage.add(contentLayer)

  /** @type {Cam} 世界原点在屏幕左上角对应的世界坐标 */
  let cam = { x: 0, y: 0, scale: 1 }
  let spaceHeld = false
  let panning = false

  const clampScale = (s) => Math.min(MAX_SCALE, Math.max(MIN_SCALE, s))
  const notify = () => options.onCamMove?.({ ...cam })

  function applyCam() {
    contentLayer.scale({ x: cam.scale, y: cam.scale })
    contentLayer.position({ x: -cam.x * cam.scale, y: -cam.y * cam.scale })
    contentLayer.batchDraw()
    applyDotGrid()
    notify()
  }

  /** 网点背景(Figma 风格):点距屏幕恒定,平移随世界移动,跨缩放阈值切换密度 */
  function applyDotGrid() {
    const step = dotStepFor(cam.scale)
    const offsetX = (((-cam.x * cam.scale) % step) + step) % step
    const offsetY = (((-cam.y * cam.scale) % step) + step) % step
    rootEl.style.backgroundPosition = `${offsetX}px ${offsetY}px`
    rootEl.style.backgroundSize = `${step}px ${step}px`
  }

  function dotStepFor(scale) {
    // 找到使世界网格(基础 18/scale)密度落在 [18, 36) 屏幕px 的 2 的幂倍步长
    const worldStep = DOT_BASE / scale
    const factor = Math.ceil(Math.log2(worldStep / DOT_BASE))
    return DOT_BASE * 2 ** Math.max(0, factor)
  }

  /** 以屏幕坐标 anchor 为锚点缩放到 target */
  function zoomToScale(target, anchor) {
    const scale = clampScale(target)
    if (scale === cam.scale) {
      return
    }
    const world = screenToWorld(anchor)
    cam = { x: world.x - anchor.x / scale, y: world.y - anchor.y / scale, scale }
    applyCam()
  }

  function zoomBy(factor, anchor) {
    zoomToScale(cam.scale * factor, anchor)
  }

  /** 指针位置;指针从未进入舞台时回退视口中心 */
  function pointerOrCenter() {
    return stage.getPointerPosition() ?? { x: stage.width() / 2, y: stage.height() / 2 }
  }

  function screenToWorld(point) {
    return { x: cam.x + point.x / cam.scale, y: cam.y + point.y / cam.scale }
  }

  function worldToScreen(point) {
    return { x: (point.x - cam.x) * cam.scale, y: (point.y - cam.y) * cam.scale }
  }

  /** 视野居中到世界坐标点(初始视图 / 100% 重置用) */
  function centerOn(world, scale = cam.scale) {
    const s = clampScale(scale)
    cam = {
      x: world.x - stage.width() / 2 / s,
      y: world.y - stage.height() / 2 / s,
      scale: s,
    }
    applyCam()
  }

  /* ---------- 手势:滚轮 / 捏合 ---------- */
  // 触控板与鼠标滚轮都派发 wheel,区分启发式:
  //   ctrlKey → 触控板捏合,缩放;
  //   deltaX ≠ 0 → 触控板双指横移,平移;
  //   deltaY 为整数且绝对值较大(≥40)→ 鼠标滚轮格,缩放;
  //   其余(小数 deltaY)→ 触控板双指纵移,平移。
  stage.on('wheel', (e) => {
    const ev = e.evt
    ev.preventDefault()
    if (ev.ctrlKey || ev.metaKey) {
      zoomBy(ev.deltaY < 0 ? 1 / WHEEL_ZOOM_STEP : WHEEL_ZOOM_STEP, pointerOrCenter())
      return
    }
    if (Math.abs(ev.deltaX) > 0 || !Number.isInteger(ev.deltaY) || Math.abs(ev.deltaY) < 40) {
      cam.x += ev.deltaX / cam.scale
      cam.y += ev.deltaY / cam.scale
      applyCam()
      return
    }
    zoomBy(ev.deltaY < 0 ? WHEEL_ZOOM_STEP : 1 / WHEEL_ZOOM_STEP, pointerOrCenter())
  })

  /* ---------- 手势:空格+拖 / 中键拖 ---------- */
  stage.on('mousedown touchstart', (e) => {
    const wantPan = spaceHeld || e.evt.button === 1
    if (!wantPan) {
      return
    }
    e.evt.preventDefault()
    panning = true
    rootEl.classList.add('panning')
    stage.stopDrag()
    stage.draggable(false)
  })
  stage.on('mousemove', (e) => {
    if (!panning || e.evt.buttons === 0) {
      return
    }
    const ev = e.evt
    cam.x -= ev.movementX / cam.scale
    cam.y -= ev.movementY / cam.scale
    applyCam()
  })
  const endPan = () => {
    panning = false
    rootEl.classList.remove('panning')
  }
  stage.on('mouseup mouseleave touchend', endPan)
  rootEl.addEventListener('contextmenu', (e) => e.preventDefault())

  window.addEventListener('keydown', (e) => {
    if (e.code === 'Space' && !spaceHeld) {
      spaceHeld = true
      rootEl.classList.add('panning')
    }
  })
  window.addEventListener('keyup', (e) => {
    if (e.code === 'Space') {
      spaceHeld = false
      if (!panning) {
        rootEl.classList.remove('panning')
      }
    }
  })

  /* ---------- 尺寸自适应 ---------- */
  const resizeObserver = new ResizeObserver(() => {
    stage.width(rootEl.clientWidth)
    stage.height(rootEl.clientHeight)
    stage.batchDraw()
  })
  resizeObserver.observe(rootEl)

  applyCam()

  return {
    stage,
    contentLayer,
    getCam: () => ({ ...cam }),
    zoomBy,
    zoomToScale,
    centerOn,
    screenToWorld,
    worldToScreen,
    clampScale,
    ZOOM_LEVELS,
    destroy() {
      resizeObserver.disconnect()
      stage.destroy()
    },
  }
}

/**
 * @typedef {Object} Cam
 * @property {number} x
 * @property {number} y
 * @property {number} scale
 *
 * @typedef {ReturnType<typeof createCanvasEngine>} CanvasEngine
 */
