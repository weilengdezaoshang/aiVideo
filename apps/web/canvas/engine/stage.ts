import { bindInteraction, createInteraction, type InteractionState } from './interaction.js'

// 画布引擎:Konva 舞台 + 相机(平移/缩放)+ 网点背景。
// 相机属 TransientStore 语义(PRD §7.2):不进 undo、不入文档,仅内存 + 订阅。
// 手势(PRD §5.2):
//   平移 —— 触控板双指 / 空格+拖 / 鼠标中键
//   缩放 —— 滚轮 / 捏合(ctrl+wheel),始终以光标为锚

export type Point = { x: number; y: number }
export type Cam = { x: number; y: number; scale: number }

export type CanvasEngineOptions = { onCamMove?: (cam: Cam) => void }

export type CanvasEngine = {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Konva 以 UMD vendor 引入,无类型来源
  stage: any
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Konva 以 UMD vendor 引入,无类型来源
  contentLayer: any
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Konva 以 UMD vendor 引入,无类型来源
  registerCamLayer(layer: any): void
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Konva 以 UMD vendor 引入,无类型来源
  unregisterCamLayer(layer: any): void
  getCam(): Cam
  /** 舞台获得有效尺寸或尺寸变化时触发;返回退订函数 */
  onSizeChanged(listener: () => void): () => void
  /** 平移手势进行中(空格按住 / 中键拖动);对象层的框选需避开 */
  interaction: ReturnType<typeof createInteraction>
  input: ReturnType<typeof bindInteraction>
  isPanActive(): boolean
  setPanTool(enabled: boolean): void
  zoomBy(factor: number, anchor: Point): void
  zoomToScale(target: number, anchor: Point): void
  centerOn(world: Point, scale?: number): void
  panBy(screenDx: number, screenDy: number): void
  screenToWorld(point: Point): Point
  worldToScreen(point: Point): Point
  clampScale(s: number): number
  ZOOM_LEVELS: number[]
  destroy(): void
}

const MIN_SCALE = 0.05
const MAX_SCALE = 8
const WHEEL_ZOOM_STEP = 1.1
/** 网点基础点距(px,屏幕坐标);缩放跨越阈值时倍增/减半,避免极端缩放下过密过疏 */
const DOT_BASE = 18
const ZOOM_LEVELS = [0.05, 0.125, 0.25, 0.5, 1, 2, 4, 8]

/** wheel/pinch 的统一缩放方向：负值放大，正值缩小。 */
export function wheelZoomFactor(deltaY: number): number {
  return deltaY < 0 ? WHEEL_ZOOM_STEP : 1 / WHEEL_ZOOM_STEP
}

/**
 * @param rootEl 画布容器(.canvas-root,网点背景作用其上)
 * @param surfaceEl Konva 舞台宿主;必须是空元素(Stage 构造会清空其内容)
 */
export function createCanvasEngine(
  rootEl: HTMLElement,
  surfaceEl: HTMLElement,
  options: CanvasEngineOptions = {},
): CanvasEngine {
  const stage = new Konva.Stage({
    container: surfaceEl,
    width: surfaceEl.clientWidth,
    height: surfaceEl.clientHeight,
  })
  /** 对象层:T6 起渲染文档对象;相机直接作用于此层 */
  const contentLayer = new Konva.Layer({ listening: true })
  stage.add(contentLayer)
  /** 需要跟随相机变换的其它层(如选中框覆盖层) */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Konva 以 UMD vendor 引入,无类型来源
  const camLayers: any[] = []

  /** 世界原点在屏幕左上角对应的世界坐标 */
  let cam: Cam = { x: 0, y: 0, scale: 1 }
  const interaction = createInteraction()

  const clampScale = (s: number) => Math.min(MAX_SCALE, Math.max(MIN_SCALE, s))
  const notify = () => options.onCamMove?.({ ...cam })

  function applyCam() {
    const tr = { x: -cam.x * cam.scale, y: -cam.y * cam.scale }
    const sc = { x: cam.scale, y: cam.scale }
    contentLayer.scale(sc)
    contentLayer.position(tr)
    for (const layer of camLayers) {
      layer.scale(sc)
      layer.position(tr)
    }
    // 同步 draw 而非 batchDraw:Konva 10 的 rAF 调度在后台标签页不触发,
    // 会导致命中画布停留在旧位置,相机移动后点不中对象
    contentLayer.draw()
    applyDotGrid()
    notify()
  }

  /**
   * 注册一个跟随相机变换的层(对象层之外,例如选中框覆盖层)。
   */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Konva 以 UMD vendor 引入,无类型来源
  function registerCamLayer(layer: any) {
    if (camLayers.includes(layer)) {
      return
    }
    camLayers.push(layer)
    layer.scale({ x: cam.scale, y: cam.scale })
    layer.position({ x: -cam.x * cam.scale, y: -cam.y * cam.scale })
    layer.batchDraw()
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Konva 以 UMD vendor 引入,无类型来源
  function unregisterCamLayer(layer: any) {
    const index = camLayers.indexOf(layer)
    if (index >= 0) {
      camLayers.splice(index, 1)
    }
  }

  /** 网点背景(Figma 风格):点距屏幕恒定,平移随世界移动,跨缩放阈值切换密度 */
  function applyDotGrid() {
    const step = dotStepFor(cam.scale)
    const offsetX = (((-cam.x * cam.scale) % step) + step) % step
    const offsetY = (((-cam.y * cam.scale) % step) + step) % step
    rootEl.style.backgroundPosition = `${offsetX}px ${offsetY}px`
    rootEl.style.backgroundSize = `${step}px ${step}px`
  }

  function dotStepFor(scale: number): number {
    // 找到使世界网格(基础 18/scale)密度落在 [18, 36) 屏幕px 的 2 的幂倍步长
    const worldStep = DOT_BASE / scale
    const factor = Math.ceil(Math.log2(worldStep / DOT_BASE))
    return DOT_BASE * 2 ** Math.max(0, factor)
  }

  /** 以屏幕坐标 anchor 为锚点缩放到 target */
  function zoomToScale(target: number, anchor: Point) {
    const scale = clampScale(target)
    if (scale === cam.scale) {
      return
    }
    const world = screenToWorld(anchor)
    cam = { x: world.x - anchor.x / scale, y: world.y - anchor.y / scale, scale }
    applyCam()
  }

  function zoomBy(factor: number, anchor: Point) {
    zoomToScale(cam.scale * factor, anchor)
  }

  function screenToWorld(point: Point): Point {
    return { x: cam.x + point.x / cam.scale, y: cam.y + point.y / cam.scale }
  }

  function worldToScreen(point: Point): Point {
    return { x: (point.x - cam.x) * cam.scale, y: (point.y - cam.y) * cam.scale }
  }

  /** 按屏幕像素平移视图(操作栏"轻移视口腾出空间"用,PRD §4.1)。 */
  function panBy(screenDx: number, screenDy: number) {
    cam.x -= screenDx / cam.scale
    cam.y -= screenDy / cam.scale
    applyCam()
  }

  /** 视野居中到世界坐标点(初始视图 / 100% 重置用) */
  function centerOn(world: Point, scale: number = cam.scale) {
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
  const input = bindInteraction(surfaceEl, interaction, {
    pan: panBy,
    wheel: (ev) => {
      const rect = surfaceEl.getBoundingClientRect()
      const anchor = { x: ev.clientX - rect.left, y: ev.clientY - rect.top }
      if (ev.ctrlKey || ev.metaKey) {
        zoomBy(wheelZoomFactor(ev.deltaY), anchor)
      } else if (
        Math.abs(ev.deltaX) > 0 ||
        !Number.isInteger(ev.deltaY) ||
        Math.abs(ev.deltaY) < 40
      ) {
        panBy(-ev.deltaX, -ev.deltaY)
      } else {
        zoomBy(wheelZoomFactor(ev.deltaY), anchor)
      }
    },
  })
  interaction.subscribe((state: InteractionState) => {
    contentLayer.listening(state.effective === 'select')
    contentLayer.drawHit()
    rootEl.classList.toggle('panning', state.effective === 'pan')
    surfaceEl.style.cursor =
      state.effective === 'pan'
        ? state.gesture
          ? 'grabbing'
          : 'grab'
        : state.mode === 'mask'
          ? 'crosshair'
          : ''
    rootEl.dataset.interactionMode = state.effective
  })

  /* ---------- 尺寸自适应 ----------
   * 观察舞台宿主本身(容器可能在模块执行后才完成布局,如隐藏面板中加载)。 */
  /** 首次拿到有效尺寸或尺寸变化时回调;文档加载早于布局收敛时,初始视角依赖它重放 */
  const sizeListeners = new Set<() => void>()
  const notifySizeChanged = () => {
    for (const listener of sizeListeners) {
      listener()
    }
  }
  const resizeObserver = new ResizeObserver(() => {
    const w = surfaceEl.clientWidth
    const h = surfaceEl.clientHeight
    if (w > 0 && h > 0 && (stage.width() !== w || stage.height() !== h)) {
      stage.width(w)
      stage.height(h)
      stage.batchDraw()
      notifySizeChanged()
    }
  })
  resizeObserver.observe(surfaceEl)
  resizeObserver.observe(rootEl)

  /** 同步舞台尺寸到容器;返回是否发生变化 */
  function syncSize(): boolean {
    const w = surfaceEl.clientWidth
    const h = surfaceEl.clientHeight
    if (w > 0 && h > 0 && (stage.width() !== w || stage.height() !== h)) {
      stage.width(w)
      stage.height(h)
      stage.batchDraw()
      notifySizeChanged()
      return true
    }
    return false
  }
  window.addEventListener('resize', syncSize)
  // 启动后前几帧布局可能仍在收敛(CSS/字体加载、宿主面板显隐):主动同步几帧
  for (let i = 1; i <= 10; i++) {
    setTimeout(syncSize, i * 120)
  }

  applyCam()

  return {
    stage,
    contentLayer,
    registerCamLayer,
    unregisterCamLayer,
    getCam: () => ({ ...cam }),
    onSizeChanged(listener: () => void) {
      sizeListeners.add(listener)
      return () => sizeListeners.delete(listener)
    },
    interaction,
    input,
    isPanActive: () => interaction.getState().effective === 'pan',
    setPanTool(enabled: boolean) {
      if (['select', 'pan'].includes(interaction.getState().mode)) {
        interaction.setMode(enabled ? 'pan' : 'select')
      }
    },
    zoomBy,
    zoomToScale,
    centerOn,
    panBy,
    screenToWorld,
    worldToScreen,
    clampScale,
    ZOOM_LEVELS,
    destroy() {
      input.destroy()
      resizeObserver.disconnect()
      stage.destroy()
    },
  }
}
