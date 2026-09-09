import { cmdAddObjects, cmdMoveObjects } from '../state/commands.js'
import { groupInsertion } from '../state/placement.js'
import { ensureAsset } from './asset-util.js'

/** @param {string} selector */
const $ = (selector) => /** @type {any} */ (document.querySelector(selector))

/** @param {string} url @returns {Promise<HTMLImageElement>} */
function loadImage(url) {
  return new Promise((resolve, reject) => {
    const image = new Image()
    image.onload = () => resolve(image)
    image.onerror = () => reject(new Error('图片加载失败'))
    image.src = url
  })
}

/** 蒙版草稿的本地持久化键(PRD §6:建议笔画结束保存草稿;与文档存储分离) */
/** @param {string} assetId */
const draftKey = (assetId) => `gencanvas.maskDraft.${assetId}`

/**
 * 智能抠图会话(PRD §6 状态机):
 *   open(识别或复用 Agent 预备蒙版)→ 蒙版编辑(画笔/橡皮/撤销/重做/草稿)→
 *   只有"抠图"按钮显式提交 → 透明 PNG 相邻落位(组内顺延)。
 * 蒙版历史与画布撤销相互隔离;保存草稿不会自动生成。
 */
/** @param {{ getDocStore: () => any, engine: any, onResult?: (info: {objId: string, assetUrl: string, sourceLabel: string, sessionId?: string, failed?: boolean}) => void, toast?: (text: string) => void }} options */
export function createCutoutSession({ getDocStore, engine, onResult = () => {}, toast = () => {} }) {
  const bar = $('#cutout-bar')
  const status = $('#cutout-status')
  const applyButton = $('#cutout-apply')
  const eraseButton = $('#cutout-erase')
  const restoreButton = $('#cutout-restore')
  const undoButton = $('#cutout-undo')
  const redoButton = $('#cutout-redo')
  const brushInput = /** @type {HTMLInputElement} */ ($('#cutout-brush'))
  const maskCanvas = document.createElement('canvas')
  const overlayCanvas = document.createElement('canvas')
  const maskContext = /** @type {CanvasRenderingContext2D} */ (maskCanvas.getContext('2d'))
  const overlayContext = /** @type {CanvasRenderingContext2D} */ (overlayCanvas.getContext('2d'))
  /** @type {any} */
  let sourceObject = null
  /** @type {any} */
  let overlayLayer = null
  /** @type {any} */
  let overlayNode = null
  let drawing = false
  let paintedSinceStroke = false
  let mode = 'erase'
  let revision = 0
  let saving = false
  let controller = new AbortController()
  /** @type {string[]} 已提交笔画快照(undo 栈,dataURL) */
  let history = []
  /** @type {string[]} */
  let redoStack = []
  /** @type {string | null} Agent 会话绑定(提交时回传服务端) */
  let agentSessionId = null

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
  function emitMaskMode(active) {
    document.dispatchEvent(new CustomEvent('gencanvas:maskmode', { detail: { active } }))
  }

  /** 静默收尾:恢复画布交互并保留草稿,不弹提示(open 切换会话时复用)。 */
  function teardown() {
    revision++
    controller.abort()
    drawing = false
    removeOverlay()
    engine.contentLayer.listening(true)
    engine.contentLayer.drawHit()
    if (sourceObject?.assetId) {
      // 蒙版历史与草稿在离开时持久化;不生成任何结果(PRD §6)
      saveDraft()
    }
    sourceObject = null
    agentSessionId = null
    history = []
    redoStack = []
    if (!bar.hidden) {
      bar.hidden = true
      emitMaskMode(false)
    }
  }

  /** 用户显式退出:保存草稿并提示(PRD §6 退出编辑态)。 */
  function exit() {
    const hadSession = Boolean(sourceObject?.assetId)
    teardown()
    if (hadSession) {
      toast('草稿已保留')
    }
  }

  /** @returns {string} 当前蒙版 dataURL */
  const snapshot = () => maskCanvas.toDataURL('image/png')

  /** @param {string} dataUrl */
  async function restoreSnapshot(dataUrl) {
    const img = await loadImage(dataUrl)
    maskContext.clearRect(0, 0, maskCanvas.width, maskCanvas.height)
    maskContext.drawImage(img, 0, 0)
    renderOverlay()
  }

  function syncHistoryButtons() {
    undoButton.disabled = history.length === 0
    redoButton.disabled = redoStack.length === 0
  }

  async function pushHistory() {
    history.push(await snapshot())
    if (history.length > 30) {
      history.shift()
    }
    redoStack = []
    syncHistoryButtons()
    saveDraftSoon()
  }

  async function undo() {
    if (history.length === 0) {
      return
    }
    redoStack.push(await snapshot())
    const prev = history.pop()
    if (prev) {
      await restoreSnapshot(prev)
    }
    syncHistoryButtons()
    saveDraftSoon()
  }

  async function redo() {
    if (redoStack.length === 0) {
      return
    }
      history.push(await snapshot())
      const next = redoStack.pop()
      if (next) {
        await restoreSnapshot(next)
      }
      syncHistoryButtons()
      saveDraftSoon()
  }

  /** 草稿防抖保存(写失败静默:草稿是本机便利,不承诺恢复) */
  /** @type {ReturnType<typeof setTimeout> | null} */
  let draftTimer = null
  function saveDraftSoon() {
    if (draftTimer) {
      clearTimeout(draftTimer)
    }
    draftTimer = setTimeout(() => saveDraft(), 800)
  }
  function saveDraft() {
    if (draftTimer) {
      clearTimeout(draftTimer)
      draftTimer = null
    }
    try {
      localStorage.setItem(draftKey(sourceObject?.assetId || ''), maskCanvas.toDataURL('image/png'))
    } catch {
      // 本机存储失败:草稿只活到关闭页
    }
  }

  function renderOverlay() {
    if (!overlayNode) {return}
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
    overlayLayer.batchDraw()
  }

  /** @param {any} obj */
  function mountOverlay(obj) {
    removeOverlay()
    overlayLayer = new Konva.Layer({ listening: false })
    overlayNode = new Konva.Image({
      x: obj.x,
      y: obj.y,
      width: obj.width,
      height: obj.height,
      image: overlayCanvas,
      listening: false,
    })
    overlayLayer.add(overlayNode)
    engine.stage.add(overlayLayer)
    engine.registerCamLayer(overlayLayer)
    engine.contentLayer.listening(false)
    engine.contentLayer.drawHit()
    renderOverlay()
  }

  /** @param {PointerEvent} event */
  function paint(event) {
    const obj = sourceObject && getDocStore()?.doc.objects[sourceObject.id]
    if (!obj || maskCanvas.width === 0) {return}
    const rect = engine.stage.container().getBoundingClientRect()
    const world = engine.screenToWorld({ x: event.clientX - rect.left, y: event.clientY - rect.top })
    if (world.x < obj.x || world.y < obj.y || world.x > obj.x + obj.width || world.y > obj.y + obj.height) {
      return
    }
    if (!paintedSinceStroke) {
      paintedSinceStroke = true
      void pushHistory()
    }
    const x = ((world.x - obj.x) / obj.width) * maskCanvas.width
    const y = ((world.y - obj.y) / obj.height) * maskCanvas.height
    const radius = (Number(brushInput.value) / obj.width) * maskCanvas.width
    maskContext.fillStyle = mode === 'erase' ? '#000' : '#fff'
    maskContext.beginPath()
    maskContext.arc(x, y, radius / 2, 0, Math.PI * 2)
    maskContext.fill()
    renderOverlay()
  }

  const container = engine.stage.container()
  container.addEventListener('pointerdown', (/** @type {PointerEvent} */ event) => {
    if (bar.hidden || saving || !overlayNode) {return}
    drawing = true
    paintedSinceStroke = false
    container.setPointerCapture(event.pointerId)
    paint(event)
  })
  container.addEventListener('pointermove', (/** @type {PointerEvent} */ event) => drawing && paint(event))
  container.addEventListener('pointerup', () => { drawing = false })
  container.addEventListener('pointercancel', () => { drawing = false })

  /** @param {'erase' | 'restore'} next */
  function setMode(next) {
    mode = next
    eraseButton.classList.toggle('active', next === 'erase')
    restoreButton.classList.toggle('active', next === 'restore')
  }
  eraseButton.addEventListener('click', () => setMode('erase'))
  restoreButton.addEventListener('click', () => setMode('restore'))
  undoButton.addEventListener('click', () => void undo())
  redoButton.addEventListener('click', () => void redo())
  $('#cutout-cancel').addEventListener('click', exit)
  document.addEventListener('keydown', (event) => {
    if (bar.hidden) {return}
    if (event.key === 'Escape') {exit()}
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'z') {
      event.preventDefault()
      if (event.shiftKey) {
        void redo()
      } else {
        void undo()
      }
    }
  })

  /**
   * 打开会话。
   * @param {any} obj
   * @param {{ maskUrl?: string, sessionId?: string } | undefined} [prepared]
   *   maskUrl:Agent 已识别的蒙版(不再调 detect,恢复后不二次识别);
   *   sessionId:提交时绑定 Agent 会话(PRD §7)。
   */
  async function open(obj, prepared = undefined) {
    if (saving) {return}
    teardown()
    const currentRevision = revision
    controller = new AbortController()
    sourceObject = { ...obj }
    agentSessionId = prepared?.sessionId ?? null
    bar.hidden = false
    emitMaskMode(true)
    try {
      const original = obj.cutout
        ? { id: obj.cutout.originalAssetId, ext: obj.cutout.originalExt }
        : await ensureAsset(obj)
      if (revision !== currentRevision) {return}
      sourceObject = { ...obj, assetId: original.id, ext: original.ext }
      if (prepared?.maskUrl) {
        status.textContent = '助手已准备蒙版,请检查主体选区'
      } else {
        status.textContent = '正在识别主体,首次使用需要下载模型…'
      }
      applyButton.disabled = true
      let mask
      if (prepared?.maskUrl) {
        mask = await loadImage(prepared.maskUrl)
        if (revision !== currentRevision) {return}
      } else {
        const response = await fetch(`/api/cutout/detect/${original.id}`, {
          method: 'POST', signal: controller.signal,
        })
        const body = await response.json()
        if (!response.ok) {throw new Error(body.error || '主体识别失败')}
        mask = await loadImage(body.urls.original)
        if (revision !== currentRevision) {return}
      }
      maskCanvas.width = overlayCanvas.width = mask.naturalWidth
      maskCanvas.height = overlayCanvas.height = mask.naturalHeight
      maskContext.clearRect(0, 0, maskCanvas.width, maskCanvas.height)
      // 已有本机草稿 → 继续草稿(PRD §6:再次进入提示继续草稿)
      let draft = null
      try {
        draft = localStorage.getItem(draftKey(original.id))
      } catch {
        // 本机存储不可用:跳过草稿恢复
      }
      if (draft) {
        const draftImg = await loadImage(draft)
        if (draftImg.naturalWidth === mask.naturalWidth && draftImg.naturalHeight === mask.naturalHeight) {
          maskContext.drawImage(draftImg, 0, 0)
          status.textContent = '已恢复上次蒙版草稿,可继续调整'
        } else {
          maskContext.drawImage(mask, 0, 0)
        }
      } else {
        maskContext.drawImage(mask, 0, 0)
      }
      history = []
      redoStack = []
      syncHistoryButtons()
      mountOverlay(getDocStore()?.doc.objects[obj.id] || obj)
      if (!draft) {
        status.textContent = prepared?.maskUrl ? '助手已准备蒙版,请检查主体选区' : '调整主体选区'
      }
      applyButton.disabled = false
    } catch (error) {
      if (revision === currentRevision && !(error instanceof DOMException && error.name === 'AbortError')) {
        // 识别失败:明确失败,不展示假蒙版(PRD A08)
        status.textContent = error instanceof Error ? error.message : String(error)
      }
    }
  }

  applyButton.addEventListener('click', async () => {
    const store = getDocStore()
    if (!store || !sourceObject?.assetId || saving || !overlayNode) {return}
    const source = { ...sourceObject }
    // 必须在 exit()/teardown() 之前捕获:它们会清空 agentSessionId
    const boundSessionId = agentSessionId
    const current = store.doc.objects[source.id]
    if (!current) {return}
    const width = current.width
    const height = current.height
    // 派生结果紧邻源图右侧;组内后续成员顺延(PRD §6,间距 24 世界单位)
    const insertion = groupInsertion(store.doc, source.id, { width, height })
    const id = `cutout-${Date.now().toString(36)}`
    store.apply(cmdAddObjects([{
      id,
      kind: 'placeholder',
      x: insertion.x,
      y: insertion.y,
      width,
      height,
      gen: { jobId: `local-${id}`, message: '正在抠图' },
      lineage: { fromId: source.id, params: { operation: 'cutout' } },
    }]))
    if (insertion.moves.length > 0) {
      store.apply(cmdMoveObjects(insertion.moves))
    }
    const maskDataUrl = maskCanvas.toDataURL('image/png')
    saving = true
    exit()
    try {
      const blob = await (await fetch(maskDataUrl)).blob()
      const sessionQuery = boundSessionId ? `?sessionId=${encodeURIComponent(boundSessionId)}` : ''
      const response = await fetch(`/api/cutout/apply/${source.assetId}${sessionQuery}`, {
        method: 'POST', headers: { 'Content-Type': 'image/png' }, body: blob,
      })
      const body = await response.json()
      if (!response.ok) {throw new Error(body.error || '抠图失败')}
      store.mutateTransient((/** @type {any} */ doc) => {
        const result = doc.objects[id]
        if (!result) {return}
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
      onResult({
        objId: id,
        assetUrl: `/assets/${body.asset.id}/t256.webp`,
        sourceLabel: '画布源图',
        sessionId: boundSessionId || undefined,
      })
      // 用过的草稿完成使命:清掉,避免下一次会话误恢复旧选区
      try {
        localStorage.removeItem(draftKey(source.assetId))
      } catch {
        // 清理失败无碍:下次会话会看到同尺寸草稿,可手动撤销
      }
    } catch (error) {
      store.mutateTransient((/** @type {any} */ doc) => {
        const result = doc.objects[id]
        if (!result) {return}
        // 生成失败:保留原草稿可修正后再次显式提交(PRD §6)
        result.kind = 'error'
        result.errorDetail = error instanceof Error ? error.message : String(error)
        delete result.gen
      })
      if (boundSessionId) {
        onResult({ objId: '', assetUrl: '', sourceLabel: '', sessionId: boundSessionId, failed: true })
      }
    } finally {
      saving = false
    }
  })

  return { open, exit }
}
