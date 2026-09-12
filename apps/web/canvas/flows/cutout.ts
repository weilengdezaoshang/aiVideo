import type { CanvasObj } from '../state/commands.js'
import type { createDocStore } from '../state/doc-store.js'
import type { createCanvasEngine } from '../engine/stage.js'
import { RecognitionOperation } from './recognition-operation.js'
type DocStore = ReturnType<typeof createDocStore>
type OverlayNode = { image(canvas: HTMLCanvasElement): void }
type OverlayLayer = {
  add(node: OverlayNode): void
  destroy(): void
  batchDraw(): void
  draw(): void
}
import { cmdAddObjects, cmdBatch, cmdMoveObjects } from '../state/commands.js'
import { groupInsertion } from '../state/placement.js'
import { ensureAsset } from './asset-util.js'
import { createRecognitionLock } from './recognition-lock.js'
import { toggleMaskRegion } from './mask-selection.js'
import { transformBounds } from '../engine/transform-geometry.js'

/** @param {string} selector */
const $ = <T extends HTMLElement = HTMLElement>(selector: string): T => {
  const element = document.querySelector<T>(selector)
  if (!element) {
    throw new Error(`缺少抠图控件: ${selector}`)
  }
  return element
}

/** @param {string} url @returns {Promise<HTMLImageElement>} */
function loadImage(url: string, signal?: AbortSignal): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image()
    const cleanup = () => {
      signal?.removeEventListener('abort', abort)
      image.onload = null
      image.onerror = null
    }
    const abort = () => {
      cleanup()
      image.src = ''
      reject(new DOMException('识别已取消', 'AbortError'))
    }
    if (signal?.aborted) {
      abort()
      return
    }
    signal?.addEventListener('abort', abort, { once: true })
    image.onload = () => {
      cleanup()
      resolve(image)
    }
    image.onerror = () => {
      cleanup()
      reject(new Error('图片加载失败'))
    }
    image.src = url
  })
}

/**
 * 智能抠图会话(PRD §6 状态机):
 *   open(识别或复用 Agent 预备蒙版)→ 蒙版编辑(画笔/橡皮/撤销/重做)→
 *   只有"抠图"按钮显式提交 → 透明 PNG 相邻落位(组内顺延)。
 * 蒙版历史与画布撤销相互隔离；退出会话后不复用选区。
 */
/** @param {{ getDocStore: () => DocStore | null, engine: ReturnType<typeof createCanvasEngine>, getSelection?: () => string[] | null, pickObject?: (world: {x:number,y:number}) => string | null | undefined, onSelectObject?: (id: string) => void, onResult?: (info: {objId: string, assetUrl: string, sourceLabel: string, sessionId?: string, failed?: boolean, missing?: boolean, assetId?: string, ext?: string, width?: number, height?: number, hasThumbs?: boolean}) => void, onSuggest?: (text: string) => void, toast?: (text: string) => void }} options */
export function createCutoutSession({
  getDocStore,
  engine,
  getSelection = () => null,
  pickObject = () => null,
  onSelectObject = () => {},
  onResult = () => {},
  onSuggest = () => {},
  toast = () => {},
}: {
  getDocStore: () => DocStore | null
  engine: ReturnType<typeof createCanvasEngine>
  getSelection?: () => string[] | null
  pickObject?: (world: { x: number; y: number }) => string | null | undefined
  onSelectObject?: (id: string) => void
  onResult?: (info: {
    objId: string
    assetUrl: string
    sourceLabel: string
    sessionId?: string
    failed?: boolean
    missing?: boolean
    assetId?: string
    ext?: string
    width?: number
    height?: number
    hasThumbs?: boolean
  }) => void
  onSuggest?: (text: string) => void
  toast?: (text: string) => void
}) {
  const bar = $('#cutout-bar')
  const status = $('#cutout-status')
  const applyButton = $<HTMLButtonElement>('#cutout-apply')
  const eraseButton = $<HTMLButtonElement>('#cutout-erase')
  const restoreButton = $<HTMLButtonElement>('#cutout-restore')
  const undoButton = $<HTMLButtonElement>('#cutout-undo')
  const redoButton = $<HTMLButtonElement>('#cutout-redo')
  const quickButton = $<HTMLButtonElement>('#cutout-quick')
  const detectButton = $<HTMLButtonElement>('#cutout-detect')
  const brushInput = $<HTMLInputElement>('#cutout-brush')
  const brushSettings = $('#cutout-brush-settings')
  const maskCanvas = document.createElement('canvas')
  const overlayCanvas = document.createElement('canvas')
  const maskContext = /** @type {CanvasRenderingContext2D} */ maskCanvas.getContext(
    '2d',
  ) as CanvasRenderingContext2D
  const overlayContext = /** @type {CanvasRenderingContext2D} */ overlayCanvas.getContext(
    '2d',
  ) as CanvasRenderingContext2D
  /** @type {any} */
  let sourceObject: CanvasObj | null = null
  /** @type {any} */
  let overlayLayer: OverlayLayer | null = null
  /** @type {any} */
  let overlayNode: OverlayNode | null = null
  let drawing = false
  let paintedSinceStroke = false
  let mode = 'restore'
  /** @type {ImageData | null} */
  let detectedMask: ImageData | null = null
  let revision = 0
  let saving = false
  let operation: RecognitionOperation | null = null
  let unsubscribeSource: (() => void) | null = null
  let sourceFrame: number | null = null
  let matchesSource: () => boolean = () => false
  const sourceKey = (obj: CanvasObj) =>
    JSON.stringify([
      obj.assetId,
      obj.src,
      obj.ext,
      obj.cutout?.originalAssetId,
      obj.cutout?.originalExt,
    ])

  function beginOperation() {
    operation?.cancel()
    const currentRevision = ++revision
    const next = new RecognitionOperation(() => revision === currentRevision && matchesSource())
    operation = next
    engine.input.setRecognitionCancel(cancelRecognition)
    engine.input.setRecognitionPointer((event) => {
      const rect = engine.stage.container().getBoundingClientRect()
      const id = pickObject(
        engine.screenToWorld({ x: event.clientX - rect.left, y: event.clientY - rect.top }),
      )
      if (id !== sourceObject?.id) {
        exit()
        if (id) {
          onSelectObject(id)
        }
      }
    })
    return next
  }

  function cancelRecognition() {
    if (!overlayNode || !sourceObject) {
      exit()
      return
    }
    revision++
    operation?.cancel()
    operation = null
    recognitionLock.unlock()
    engine.input.setRecognitionPointer(null)
    engine.interaction.setMode('mask', sourceObject.id)
    installMaskInput()
    bar.dataset.phase = 'ready'
    emitMaskMode(true)
  }
  /** @type {ImageData[]} 已提交笔画快照 */
  let history: ImageData[] = []
  /** @type {ImageData[]} */
  let redoStack: ImageData[] = []
  /** @type {string | null} Agent 会话绑定(提交时回传服务端) */
  let agentSessionId: string | null = null
  const recognitionLock = createRecognitionLock({
    onCancel: cancelRecognition,
    getAnchor: () => {
      if (sourceObject && !matchesSource()) {
        exit()
        return null
      }
      const obj = sourceObject && getDocStore()?.doc.objects[sourceObject.id]
      if (!obj) {
        return null
      }
      const bounds = transformBounds(obj)
      const screen = engine.worldToScreen(bounds)
      const rect = engine.stage.container().getBoundingClientRect()
      const scale = engine.getCam().scale
      const left = Math.max(rect.left, rect.left + screen.x)
      const top = Math.max(rect.top, rect.top + screen.y)
      const right = Math.min(rect.right, rect.left + screen.x + bounds.width * scale)
      const bottom = Math.min(rect.bottom, rect.top + screen.y + bounds.height * scale)
      return { left, top, width: Math.max(0, right - left), height: Math.max(0, bottom - top) }
    },
  })

  function removeOverlay() {
    if (overlayLayer) {
      engine.unregisterCamLayer?.(overlayLayer)
      overlayLayer.destroy()
    }
    overlayLayer = null
    overlayNode = null
  }

  /** 通知底部输入区让位/恢复(蒙版栏与输入区同处底部,PRD §4.1 安全区) */
  /** @param {boolean} active */
  function emitMaskMode(active: boolean) {
    document.dispatchEvent(new CustomEvent('gencanvas:maskmode', { detail: { active } }))
  }

  /** 静默收尾：恢复画布交互，丢弃本次选区与笔画历史。 */
  function teardown() {
    closeBrushSettings()
    revision++
    recognitionLock.unlock()
    operation?.cancel()
    operation = null
    unsubscribeSource?.()
    unsubscribeSource = null
    if (sourceFrame !== null) {
      cancelAnimationFrame(sourceFrame)
      sourceFrame = null
    }
    engine.input.setRecognitionPointer(null)
    drawing = false
    engine.interaction.setMode('select')
    engine.input.setMaskHandlers(null)
    removeOverlay()
    sourceObject = null
    detectedMask = null
    delete bar.dataset.sourceId
    agentSessionId = null
    history = []
    redoStack = []
    if (!bar.hidden) {
      bar.hidden = true
      emitMaskMode(false)
    }
  }

  /** 退出后再次抠图必须建立新识别会话。 */
  function exit() {
    teardown()
  }

  const pixels = () => maskContext.getImageData(0, 0, maskCanvas.width, maskCanvas.height)
  /** @param {ImageData} data */
  function restoreSnapshot(data: ImageData) {
    maskContext.putImageData(data, 0, 0)
    renderOverlay()
  }

  function syncHistoryButtons() {
    undoButton.disabled = history.length === 0
    redoButton.disabled = redoStack.length === 0
  }

  function pushHistory() {
    history.push(strokePixels)
    const limit = Math.max(
      1,
      Math.min(30, Math.floor((64 * 1024 * 1024) / (maskCanvas.width * maskCanvas.height * 4))),
    )
    if (history.length > limit) {
      history.shift()
    }
    redoStack = []
    syncHistoryButtons()
  }

  function undo() {
    if (history.length === 0) {
      return
    }
    redoStack.push(pixels())
    const prev = history.pop()
    if (prev) {
      restoreSnapshot(prev)
    }
    syncHistoryButtons()
  }

  function redo() {
    if (redoStack.length === 0) {
      return
    }
    history.push(pixels())
    const next = redoStack.pop()
    if (next) {
      restoreSnapshot(next)
    }
    syncHistoryButtons()
  }

  function renderOverlay() {
    if (!overlayNode) {
      return
    }
    const mask = maskContext.getImageData(0, 0, maskCanvas.width, maskCanvas.height).data
    const overlay = overlayContext.createImageData(overlayCanvas.width, overlayCanvas.height)
    for (let i = 0; i < mask.length; i += 4) {
      overlay.data[i] = 255
      overlay.data[i + 1] = 65
      overlay.data[i + 2] = 75
      overlay.data[i + 3] = Math.round(mask[i] * 0.42)
    }
    overlayContext.putImageData(overlay, 0, 0)
    overlayNode.image(overlayCanvas)
    overlayLayer?.batchDraw()
  }

  /** @param {any} obj */
  function mountOverlay(obj: CanvasObj) {
    removeOverlay()
    overlayLayer = new Konva.Layer({ listening: false }) as OverlayLayer
    overlayNode = new Konva.Image({
      x: obj.x,
      y: obj.y,
      width: obj.width,
      height: obj.height,
      rotation: obj.rotation || 0,
      image: overlayCanvas,
      listening: false,
    }) as OverlayNode
    overlayLayer.add(overlayNode)
    engine.stage.add(overlayLayer)
    engine.registerCamLayer(overlayLayer)

    renderOverlay()
    overlayLayer.draw()
  }

  /** @param {PointerEvent} event */
  function paint(event: PointerEvent) {
    const obj = sourceObject && getDocStore()?.doc.objects[sourceObject.id]
    if (!obj || maskCanvas.width === 0) {
      return
    }
    const p = sourcePoint(event, obj)
    if (!pointInSource(event, obj)) {
      return
    }
    if (!paintedSinceStroke) {
      paintedSinceStroke = true
      strokePixels = maskContext.getImageData(0, 0, maskCanvas.width, maskCanvas.height)
      void pushHistory()
    }
    const x = (p.x / obj.width) * maskCanvas.width
    const y = (p.y / obj.height) * maskCanvas.height
    const radius = (Number(brushInput.value) / obj.width) * maskCanvas.width
    maskContext.fillStyle = mode === 'erase' ? '#000' : '#fff'
    maskContext.beginPath()
    maskContext.arc(x, y, radius / 2, 0, Math.PI * 2)
    maskContext.fill()
    renderOverlay()
  }

  /** @type {ImageData[] | null} */
  let historyBefore: ImageData[] | null = null
  /** @type {ImageData[]} */
  let redoBefore: ImageData[] = []
  function installMaskInput() {
    engine.input.setMaskHandlers({
      beforeDown: (/** @type {PointerEvent} */ event: PointerEvent) => {
        const rect = engine.stage.container().getBoundingClientRect()
        const world = engine.screenToWorld({
          x: event.clientX - rect.left,
          y: event.clientY - rect.top,
        })
        const id = pickObject(world)
        if (id !== sourceObject?.id) {
          exit()
          if (id) {
            onSelectObject(id)
          }
        }
      },
      down: (/** @type {PointerEvent} */ event: PointerEvent) => {
        const obj = sourceObject && getDocStore()?.doc.objects[sourceObject.id]
        if (!obj || !pointInSource(event, obj)) {
          return
        }
        if (mode === 'quick') {
          if (!detectedMask) {
            return
          }
          const p = sourcePoint(event, obj)
          const next = pixels()
          strokePixels = pixels()
          if (
            toggleMaskRegion(
              detectedMask,
              next,
              (p.x / obj.width) * maskCanvas.width,
              (p.y / obj.height) * maskCanvas.height,
            )
          ) {
            pushHistory()
            restoreSnapshot(next)
          } else {
            toast('请点击已识别的主体；未识别区域可用画笔补选')
          }
          return
        }
        drawing = true
        paintedSinceStroke = false
        historyBefore = history.slice()
        redoBefore = redoStack.slice()
        paint(event)
      },
      move: (/** @type {PointerEvent} */ event: PointerEvent) => {
        if (drawing) {
          paint(event)
        }
      },
      up: () => {
        drawing = false
        historyBefore = null
        redoBefore = []
      },
      cancel: () => {
        if (drawing && historyBefore) {
          // 同步回滚当前笔画；已完成的笔画不受影响。
          maskContext.putImageData(strokePixels, 0, 0)
          history = historyBefore
          redoStack = redoBefore
          renderOverlay()
          syncHistoryButtons()
        }
        drawing = false
        historyBefore = null
      },
      key: (/** @type {KeyboardEvent} */ event: KeyboardEvent) => {
        if (event.key === 'Escape') {
          if (!brushSettings.hidden) {
            closeBrushSettings()
          } else {
            exit()
          }
        } else if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'z') {
          if (event.shiftKey) {
            void redo()
          } else {
            void undo()
          }
        }
      },
    })
  }
  /** @type {ImageData} */
  let strokePixels: ImageData
  /** @param {PointerEvent} event @param {any} obj */
  function sourcePoint(event: PointerEvent, obj: CanvasObj) {
    const rect = engine.stage.container().getBoundingClientRect()
    const world = engine.screenToWorld({
      x: event.clientX - rect.left,
      y: event.clientY - rect.top,
    })
    const angle = ((obj.rotation || 0) * Math.PI) / 180
    const dx = world.x - obj.x,
      dy = world.y - obj.y
    return {
      x: dx * Math.cos(angle) + dy * Math.sin(angle),
      y: -dx * Math.sin(angle) + dy * Math.cos(angle),
    }
  }
  /** @param {PointerEvent} event @param {any} obj */
  function pointInSource(event: PointerEvent, obj: CanvasObj) {
    const p = sourcePoint(event, obj)
    return p.x >= 0 && p.y >= 0 && p.x <= obj.width && p.y <= obj.height
  }

  /** @param {'erase' | 'restore' | 'quick'} next */
  function setMode(next: 'erase' | 'restore' | 'quick') {
    mode = next
    eraseButton.classList.toggle('active', next === 'erase')
    restoreButton.classList.toggle('active', next === 'restore')
    quickButton.classList.toggle('active', next === 'quick')
    for (const [button, selected] of [
      [eraseButton, next === 'erase'],
      [restoreButton, next === 'restore'],
      [quickButton, next === 'quick'],
    ] as const) {
      button.setAttribute('aria-pressed', String(selected))
    }
    closeBrushSettings()
    status.textContent =
      next === 'quick' ? '点击主体增减选区' : next === 'erase' ? '涂抹移除选区' : '涂抹添加选区'
    emitMaskMode(true)
  }
  function closeBrushSettings() {
    brushSettings.hidden = true
    restoreButton.setAttribute('aria-expanded', 'false')
    eraseButton.setAttribute('aria-expanded', 'false')
  }
  /** 再次点击当前画笔工具打开大小设置。 @param {'erase'|'restore'} next */
  function selectBrush(next: 'erase' | 'restore') {
    const show = mode === next && brushSettings.hidden
    setMode(next)
    brushSettings.hidden = !show
    const below = bar.offsetTop < 70
    brushSettings.style.top = below ? 'calc(100% + 10px)' : 'auto'
    brushSettings.style.bottom = below ? 'auto' : 'calc(100% + 10px)'
    const button = next === 'erase' ? eraseButton : restoreButton
    button.setAttribute('aria-expanded', String(show))
  }
  document.addEventListener(
    'pointerdown',
    (event) => {
      if (!(event.target instanceof Node) || !bar.contains(event.target)) {
        closeBrushSettings()
      }
    },
    { capture: true },
  )
  brushSettings.addEventListener('keydown', (/** @type {KeyboardEvent} */ event: KeyboardEvent) => {
    if (event.key === 'Escape') {
      event.preventDefault()
      event.stopPropagation()
      closeBrushSettings()
    }
  })
  eraseButton.addEventListener('click', () => selectBrush('erase'))
  restoreButton.addEventListener('click', () => selectBrush('restore'))
  quickButton.addEventListener('click', () => setMode('quick'))
  detectButton.addEventListener('click', () => void recognizeAgain())
  undoButton.addEventListener('click', () => void undo())
  redoButton.addEventListener('click', () => void redo())
  $('#cutout-cancel').addEventListener('click', exit)
  window.addEventListener('pagehide', exit)
  async function recognizeAgain() {
    if (!sourceObject?.assetId || engine.interaction.getState().mode !== 'mask' || drawing) {
      return
    }
    closeBrushSettings()
    const active = beginOperation()
    const id = sourceObject.id
    engine.interaction.setMode('recognizing', id)
    bar.dataset.phase = 'detecting'
    recognitionLock.lock()
    try {
      const mask = await loadImage(await active.detect(sourceObject.assetId), active.signal)
      active.assertCurrent()
      strokePixels = pixels()
      maskContext.clearRect(0, 0, maskCanvas.width, maskCanvas.height)
      maskContext.drawImage(mask, 0, 0, maskCanvas.width, maskCanvas.height)
      detectedMask = pixels()
      pushHistory()
      renderOverlay()
      status.textContent = '主体已识别，可继续调整'
    } catch (error) {
      if (active.current()) {
        toast(error instanceof Error ? error.message : '识别失败，请重试')
      }
    } finally {
      if (active.current()) {
        engine.interaction.setMode('mask', id)
        installMaskInput()
        bar.dataset.phase = 'ready'
        recognitionLock.unlock()
        engine.input.setRecognitionPointer(null)
        active.complete()
        emitMaskMode(true)
      }
    }
  }
  /**
   * 打开会话。
   * @param {any} obj
   * @param {{ maskUrl?: string, sessionId?: string } | undefined} [prepared]
   *   maskUrl:Agent 已识别的蒙版(不再调 detect,恢复后不二次识别);
   *   sessionId:提交时绑定 Agent 会话(PRD §7)。
   */
  async function open(
    obj: CanvasObj,
    prepared: { maskUrl?: string; sessionId?: string } | undefined = undefined,
  ) {
    if (saving || !['select', 'pan'].includes(engine.interaction.getState().mode)) {
      return
    }
    teardown()
    const store = getDocStore()
    if (!store || !store.doc.objects[obj.id]) {
      return
    }
    onSelectObject(obj.id)
    const identity = sourceKey(obj)
    matchesSource = () => {
      const selected = getSelection()
      return (
        getDocStore() === store &&
        !!store.doc.objects[obj.id] &&
        sourceKey(store.doc.objects[obj.id]) === identity &&
        (selected === null || (selected.length === 1 && selected[0] === obj.id))
      )
    }
    unsubscribeSource = store.subscribe(() => {
      if (!matchesSource()) {
        exit()
      }
    })
    sourceObject = { ...obj }
    const watchSource = () => {
      if (!matchesSource()) {
        exit()
        return
      }
      sourceFrame = requestAnimationFrame(watchSource)
    }
    sourceFrame = requestAnimationFrame(watchSource)
    const active = beginOperation()
    agentSessionId = prepared?.sessionId ?? null
    bar.dataset.sourceId = obj.id
    engine.interaction.setMode('recognizing', obj.id)
    bar.dataset.phase = 'detecting'
    status.textContent = ''
    status.setAttribute('role', 'status')
    applyButton.disabled = true
    syncHistoryButtons()
    bar.hidden = false
    emitMaskMode(true)
    recognitionLock.lock(prepared?.maskUrl ? '正在加载选区…' : '正在识别主体…')
    try {
      const original = obj.cutout
        ? { id: obj.cutout.originalAssetId, ext: obj.cutout.originalExt }
        : await ensureAsset(obj, active.signal)
      active.assertCurrent()
      sourceObject = { ...obj, assetId: original.id, ext: original.ext }
      applyButton.disabled = true
      let mask
      if (prepared?.maskUrl) {
        mask = await loadImage(prepared.maskUrl, active.signal)
        active.assertCurrent()
      } else {
        mask = await loadImage(await active.detect(original.id), active.signal)
        active.assertCurrent()
      }
      maskCanvas.width = overlayCanvas.width = mask.naturalWidth
      maskCanvas.height = overlayCanvas.height = mask.naturalHeight
      maskContext.clearRect(0, 0, maskCanvas.width, maskCanvas.height)
      maskContext.drawImage(mask, 0, 0)
      detectedMask = pixels()
      history = []
      redoStack = []
      syncHistoryButtons()
      mountOverlay(getDocStore()?.doc.objects[obj.id] || obj)
      status.textContent = prepared?.maskUrl
        ? '助手已准备蒙版,请检查主体选区'
        : '主体已识别，确认选区后点击抠图'
      engine.interaction.setMode('mask', obj.id)
      installMaskInput()
      bar.dataset.phase = 'ready'
      setMode('restore')
      applyButton.disabled = false
      emitMaskMode(true)
    } catch (error) {
      if (active.current() && !(error instanceof DOMException && error.name === 'AbortError')) {
        // 识别失败:明确失败,不展示假蒙版(PRD A08)
        engine.interaction.setMode('select')
        bar.dataset.phase = 'error'
        status.textContent = error instanceof Error ? error.message : String(error)
      }
    } finally {
      if (active.current()) {
        recognitionLock.unlock()
        engine.input.setRecognitionPointer(null)
        active.complete()
      }
    }
  }

  applyButton.addEventListener('click', async () => {
    const store = getDocStore()
    if (!store || !sourceObject?.assetId || saving || !overlayNode) {
      return
    }
    const source = { ...sourceObject, assetId: sourceObject.assetId }
    // 必须在 exit()/teardown() 之前捕获:它们会清空 agentSessionId
    const boundSessionId = agentSessionId
    const current = store.doc.objects[source.id]
    if (!current) {
      return
    }
    const width = current.width
    const height = current.height
    // 派生结果紧邻源图右侧;组内后续成员顺延(PRD §6,间距 24 世界单位)
    const insertion = groupInsertion(store.doc, source.id, { width, height })
    const id = `cutout-${Date.now().toString(36)}`
    // 新增派生对象与组内顺延位移作为一个原子命令:一次撤销同时撤掉两者(A09)
    /** @type {any[]} */
    const commands: (ReturnType<typeof cmdAddObjects> | ReturnType<typeof cmdMoveObjects>)[] = [
      cmdAddObjects([
        {
          id,
          kind: 'placeholder',
          x: insertion.x,
          y: insertion.y,
          width,
          height,
          gen: { jobId: `local-${id}`, message: '正在抠图' },
          lineage: { fromId: source.id, params: { operation: 'cutout' } },
        },
      ]),
    ]
    if (insertion.moves.length > 0) {
      commands.push(cmdMoveObjects(insertion.moves))
    }
    store.apply(commands.length > 1 ? cmdBatch(commands) : commands[0])
    const maskDataUrl = maskCanvas.toDataURL('image/png')
    saving = true
    teardown()
    try {
      const blob = await (await fetch(maskDataUrl)).blob()
      const sessionQuery = boundSessionId ? `?sessionId=${encodeURIComponent(boundSessionId)}` : ''
      const response = await fetch(`/api/cutout/apply/${source.assetId}${sessionQuery}`, {
        method: 'POST',
        headers: { 'Content-Type': 'image/png' },
        body: blob,
      })
      const body = await response.json()
      if (!response.ok) {
        throw new Error(body.error || '抠图失败')
      }
      let landed = true
      store.mutateTransient((/** @type {any} */ doc) => {
        const result = doc.objects[id]
        if (!result) {
          // 源对象/占位在提交期间被删除:资产已生成但未落位(PRD §6)
          landed = false
          return
        }
        result.kind = 'image'
        result.assetId = body.asset.id
        result.ext = body.asset.ext
        result.hasThumbs = body.asset.hasThumbs
        result.cutout = {
          originalAssetId: source.assetId,
          originalExt: source.ext || 'png',
          maskAssetId: body.mask.id,
        }
        delete result.gen
      })
      if (landed) {
        // 扩展与其他组相交时给出"整理位置"建议(PRD §6):只提示,不自动搬动用户布局
        const result = store.doc.objects[id]
        const srcGroup = source.groupId || ''
        const crossed = Object.values(store.doc.objects).find((/** @type {any} */ o) => {
          if (o.id === id || o.id === source.id) {
            return false
          }
          if ((o.groupId || '') === srcGroup) {
            return false
          }
          return !(
            o.x >= result.x + result.width ||
            o.x + o.width <= result.x ||
            o.y >= result.y + result.height ||
            o.y + o.height <= result.y
          )
        })
        if (crossed) {
          onSuggest('派生结果与画布上的其他对象重叠,建议整理位置后再继续。')
        }
      }
      onResult({
        objId: landed ? id : '',
        assetUrl: `/assets/${body.asset.id}/t256.webp`,
        sourceLabel: '画布源图',
        sessionId: boundSessionId || undefined,
        missing: !landed,
        assetId: body.asset.id,
        ext: body.asset.ext,
        width: body.asset.width,
        height: body.asset.height,
        hasThumbs: body.asset.hasThumbs,
      })
    } catch (error) {
      store.mutateTransient((/** @type {any} */ doc) => {
        const result = doc.objects[id]
        if (!result) {
          return
        }
        // 生成失败：保留错误信息；再次抠图时重新识别
        result.kind = 'error'
        result.errorDetail = error instanceof Error ? error.message : String(error)
        delete result.gen
      })
      if (boundSessionId) {
        onResult({
          objId: '',
          assetUrl: '',
          sourceLabel: '',
          sessionId: boundSessionId,
          failed: true,
        })
      }
    } finally {
      saving = false
    }
  })

  return { open, exit }
}
