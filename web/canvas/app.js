// GenCanvas 画布入口(ESM):组装引擎、文档状态层、自动保存、提示词面板。
// 对象渲染(T6)、SSE 对账(T7)、生成闭环(T8)按 docs/开发计划-GenCanvas.md 逐步落地。

import { createCanvasEngine } from './engine/stage.js'
import { createDocStore } from './state/doc-store.js'
import { createAutoSaver } from './state/autosave.js'
import { openDocument } from './state/document-api.js'
import { createObjectLayer } from './engine/object-layer.js'
import { connectGenerationEvents } from './state/generation-events.js'
import { submitGeneration, cancelPlaceholder } from './flows/generate.js'
import { initContextMenu } from './flows/context-menu.js'
import { createInpaintSession } from './flows/inpaint.js'
import { initSettings } from './settings.js'

/** @param {string} sel @returns {HTMLElement} */
const $ = (sel) => /** @type {HTMLElement} */ (document.querySelector(sel))

const rootEl = $('#canvas-root')
const surfaceEl = $('#canvas-surface')
const promptPanel = $('#prompt-panel')
/** @type {any} 拿到 sl-textarea,聚焦走其 shadow 内原生节点 */
const promptInput = /** @type {any} */ ($('#prompt-input'))
const canvasEmpty = $('#canvas-empty')
const zoomValue = $('#zoom-value')
const saveStatusEl = $('#save-status')

/** @type {ReturnType<typeof createObjectLayer> | null} 引擎构造时会触发 onCamMove,须先声明 */
let objectLayer = null
/** @type {string[]} 当前选中(Transient) */
let selection = []

const engine = createCanvasEngine(rootEl, surfaceEl, {
  /** @param {{ scale: number }} cam */
  onCamMove: (cam) => {
    zoomValue.textContent = `${Math.round(cam.scale * 100)}%`
    objectLayer?.recomputeVisibility()
  },
})
// 初始视野:世界原点居中;文档加载后若已有对象会重定位(T6)
engine.centerOn({ x: 0, y: 0 }, 1)

// 缩放控件:以视口中心为锚
const viewportCenter = () => ({ x: engine.stage.width() / 2, y: engine.stage.height() / 2 })
$('#zoom-in').addEventListener('click', () => engine.zoomBy(1.2, viewportCenter()))
$('#zoom-out').addEventListener('click', () => engine.zoomBy(1 / 1.2, viewportCenter()))

/* ---------- 文档状态层与自动保存(T5) ---------- */

/** @type {ReturnType<typeof createDocStore> | null} */
let docStore = null
/** @type {ReturnType<typeof connectGenerationEvents> | null} T14 断线横幅将使用 */
let generationEvents = null
let centeredOnce = false

/** @param {"idle" | "saving" | "saved" | "error"} status @param {string} [detail] */
function updateSaveStatus(status, detail) {
  saveStatusEl.classList.toggle('pending', status === 'saving' || status === 'error')
  if (status === 'saving') {
    saveStatusEl.textContent = '保存中…'
  } else if (status === 'saved') {
    saveStatusEl.textContent = '已保存'
  } else if (status === 'error') {
    saveStatusEl.textContent = '保存失败'
    if (detail) {
      saveStatusEl.title = detail
    }
  } else {
    saveStatusEl.textContent = '已保存'
  }
}

/** ?stress=N 压测模式(F1):合成文档不落盘,N 个对象 + FPS 计;详见 stress.js */
const stressCount = Math.max(
  0,
  Math.floor(Number(new URLSearchParams(location.search).get('stress')) || 0),
)

async function bootDocument() {
  const doc =
    stressCount > 0
      ? { id: 'stress', name: '压力测试', objects: {}, order: [] }
      : await openDocument()
  const store = createDocStore(doc)
  const saver =
    stressCount > 0
      ? null
      : createAutoSaver({
          docId: doc.id,
          getDoc: () => store.doc,
          onStatus: updateSaveStatus,
        })

  // 对象层:渲染 + 选择/拖动/删除(T6)
  const objects = createObjectLayer({
    engine,
    getDoc: () => store.doc,
    applyCommand: (cmd) => store.apply(cmd),
    onSelectionChange: (ids) => {
      selection = ids
    },
    onCancelPlaceholder: (objId) => {
      void cancelPlaceholder({ docStore: store, objId })
    },
    onDeletePlaceholder: (objId) => {
      void cancelPlaceholder({ docStore: store, objId })
    },
    onRetryError: (objId) => {
      void import('./flows/generate.js').then((m) =>
        m.retryError({
          docStore: store,
          objId,
          resolveReference: async (fromId) => {
            const ref = store.doc.objects[fromId]
            if (!ref || (ref.kind !== 'image' && ref.kind !== 'video')) {
              return null
            }
            return { dataUrl: await objectToDataUrl(ref) }
          },
        }),
      )
    },
    imageUrlsFor: (obj) => {
      if (!obj.assetId) {
        return { original: obj.src || '' }
      }
      const base = `/assets/${obj.assetId}`
      return {
        thumb256: obj.hasThumbs === false ? null : `${base}/t256.webp`,
        thumb1024: obj.hasThumbs === false ? null : `${base}/t1024.webp`,
        original: `${base}/original.${obj.ext || 'png'}`,
      }
    },
  })
  objectLayer = objects
  objects.sync() // 首次渲染:订阅只在变更时触发,纯加载(无变更)也需要建组
  store.subscribe(({ doc: current }) => {
    objects.sync()
    saver?.markDirty()
    canvasEmpty.hidden = current.order.length > 0
    // 已有对象时初始视野框住全部内容
    if (!centeredOnce && current.order.length > 0) {
      centeredOnce = true
      fitToContent(current)
    }
  })
  canvasEmpty.hidden = doc.order.length > 0
  if (doc.order.length > 0) {
    centeredOnce = true
    fitToContent(doc)
  }
  if (saver) {
    window.addEventListener('beforeunload', () => saver.flushOnUnload())
  }
  docStore = store
  inpaint = createInpaintSession({
    engine,
    getDocStore: () => store,
    onSubmit: async ({ srcObj, maskDataUrl, prompt }) => {
      const model = /** @type {any} */ ($('#prompt-model'))?.value || undefined
      const genParams = {
        prompt,
        negativePrompt: '',
        model,
        width: srcObj.width,
        height: srcObj.height,
        steps: 20,
        cfgScale: 7,
        seed: -1,
        sampler: 'euler',
        scheduler: 'normal',
        denoise: 1,
        kind: 'image',
        durationSec: 4,
        fps: 16,
        inpaint: true,
      }
      const initImage = { dataUrl: await objectToDataUrl(srcObj) }
      await submitGeneration({
        docStore: store,
        anchor: { x: srcObj.x + srcObj.width + 40, y: srcObj.y },
        genParams,
        initImage,
        maskImage: { dataUrl: maskDataUrl },
        lineage: { fromId: srcObj.id, params: { ...genParams } },
      })
    },
  })
  contextMenu.attach(engine, () => objectLayer)
  // 右键菜单「局部重绘」:仅图像对象可用
  document.querySelector('#ctx-inpaint')?.addEventListener('click', () => {
    const sel = objectLayer?.getSelected() ?? []
    const obj = sel.length > 0 ? store.doc.objects[sel[0]] : null
    if (obj && obj.kind === 'image' && inpaint?.enter(obj.id)) {
      setInpaintUi(true)
    }
  })
  // 双击图像进入重绘(PRD §5.3-③)。Konva 不透传原生 dblclick(由 click 时序合成),
  // 这里直接绑容器 DOM 事件并用事件坐标做命中。
  engine.stage.container().addEventListener('dblclick', (/** @type {MouseEvent} */ e) => {
    if (!inpaint || inpaint.isActive()) {
      return
    }
    const rect = engine.stage.container().getBoundingClientRect()
    const pos = { x: e.clientX - rect.left, y: e.clientY - rect.top }
    const hit = engine.contentLayer.getIntersection(pos)
    if (!hit) {
      return
    }
    let node = hit
    while (node && !store.doc.objects[node.id()]) {
      node = node.parent
    }
    const obj = node ? store.doc.objects[node.id()] : null
    if (obj && obj.kind === 'image' && inpaint.enter(node.id())) {
      setInpaintUi(true)
    }
  })
  // SSE 生成事件驱动占位状态机(T7):进度/落位/失败/对账
  generationEvents = connectGenerationEvents({
    docStore: store,
    onConnectionChange: (connected) => {
      rootEl.title = connected ? '' : '连接断开,正在重连…'
    },
    onReconciled: ({ changed }) => {
      if (changed > 0) {
        objects.sync()
      }
    },
  })
  if (stressCount > 0) {
    const { installStress } = await import('./stress.js')
    await installStress(stressCount, { store })
    fitToContent(store.doc)
  }
}

/** 视野框住全部对象(首次加载) @param {{ objects: Record<string, any> }} doc */
function fitToContent(doc) {
  const list = Object.values(doc.objects)
  if (list.length === 0) {
    return
  }
  const minX = Math.min(...list.map((o) => o.x))
  const minY = Math.min(...list.map((o) => o.y))
  const maxX = Math.max(...list.map((o) => o.x + o.width))
  const maxY = Math.max(...list.map((o) => o.y + o.height))
  const margin = 80
  const scale = Math.min(
    1,
    (engine.stage.width() - margin * 2) / (maxX - minX),
    (engine.stage.height() - margin * 2) / (maxY - minY),
  )
  engine.centerOn({ x: (minX + maxX) / 2, y: (minY + maxY) / 2 }, Math.max(0.05, scale))
}

void bootDocument()

/* ---------- BYOK 设置 + 首启引导(T13,线框 S1/S2) ---------- */

void initSettings()

/* ---------- 模型列表:顶栏与提示词面板共用( provider 决定可选项) ---------- */

async function loadModels() {
  try {
    await customElements.whenDefined('sl-select')
    const res = await fetch('/api/models')
    const body = await res.json()
    const models = body.models || []
    const selects = [$('#model-select'), $('#prompt-model')]
    for (const sel of selects) {
      sel.innerHTML = models
        .map((/** @type {any} */ m) => `<sl-option value="${m.id}">${m.name}</sl-option>`)
        .join('')
    }
    if (models.length > 0) {
      // sl-select 升级与选项注册都是异步的:轮询赋值直到生效(上限 ~2s)
      await customElements.whenDefined('sl-option')
      const topSelect = /** @type {any} */ ($('#model-select'))
      const panelSelect = /** @type {any} */ ($('#prompt-model'))
      for (let i = 0; i < 40; i++) {
        topSelect.value = models[0].id
        panelSelect.value = models[0].id
        if (topSelect.value === models[0].id && panelSelect.value === models[0].id) {
          break
        }
        await new Promise((r) => setTimeout(r, 50))
      }
    }
  } catch {
    // 模型列表失败不阻塞:提交时服务端会给出明确错误
  }
}
void loadModels()

/* ---------- 撤销 / 重做快捷键(PRD §8.3,入口形式自定:Cmd/Ctrl+Z) ---------- */

/* ---------- 提示词面板:Tab 呼出 / Esc 收起(线框 S3→S4) ---------- */

/** @param {EventTarget | null} el */
const isEditable = (el) =>
  el instanceof HTMLElement &&
  (el.tagName === 'INPUT' ||
    el.tagName === 'TEXTAREA' ||
    el.tagName === 'SL-INPUT' ||
    el.tagName === 'SL-TEXTAREA' ||
    el.isContentEditable ||
    el.closest?.('sl-input, sl-textarea, [contenteditable]') != null)

/** @param {boolean} open */
function setPanelOpen(open) {
  promptPanel.hidden = !open
  if (open) {
    // Shoelace textarea 的聚焦走内部原生元素
    const input = promptInput?.shadowRoot?.querySelector('textarea') ?? promptInput
    queueMicrotask(() => input?.focus())
  }
}

document.addEventListener('keydown', (/** @type {KeyboardEvent} */ e) => {
  // 撤销/重做在输入框内生效(shadow 内 textarea 的编辑也走浏览器原生 undo)
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'z' && !isEditable(e.target)) {
    e.preventDefault()
    if (e.shiftKey) {
      docStore?.redo()
    } else {
      docStore?.undo()
    }
    return
  }
  if (
    (e.key === 'Delete' || e.key === 'Backspace') &&
    !isEditable(e.target) &&
    selection.length > 0
  ) {
    e.preventDefault()
    objectLayer?.deleteSelected()
    return
  }
  if (e.key === 'Tab' && !isEditable(e.target)) {
    e.preventDefault()
    setPanelOpen(promptPanel.hidden)
  }
  if (e.key === 'Escape' && !promptPanel.hidden && !isEditable(e.target)) {
    setPanelOpen(false)
  }
})

$('#prompt-collapse').addEventListener('click', () => setPanelOpen(false))

const getDocStore = () => docStore
const getObjectLayer = () => objectLayer
const getGenerationEvents = () => generationEvents

/* ---------- 文生图提交(T8,线框 S4→S5) ---------- */

/* ---------- 局部重绘(T11,线框 S10):双击图或菜单进入 ---------- */

/** @type {any} 局部重绘会话(TS 对 object-literal getter 推导不稳,放宽) */
let inpaint = null

const inpaintBar = $('#inpaint-bar')

/** @param {boolean} active */
function setInpaintUi(active) {
  inpaintBar.hidden = !active
  if (active) {
    setPanelOpen(false)
  }
}

$('#inpaint-brush-size').addEventListener('input', (e) => {
  inpaint?.setBrush(Number(/** @type {HTMLInputElement} */ (e.target).value))
})
$('#inpaint-erase').addEventListener('click', () => {
  if (!inpaint) {
    return
  }
  const on = !inpaint.eraseOn
  inpaint.setErase(on)(/** @type {any} */ document.querySelector('#inpaint-erase')).variant = on
    ? 'primary'
    : 'default'
})
$('#inpaint-clear').addEventListener('click', () => inpaint?.clearStrokes())
$('#inpaint-exit').addEventListener('click', () => {
  inpaint?.exit()
  setInpaintUi(false)
})
$('#inpaint-generate').addEventListener('click', async () => {
  if (!inpaint || !inpaint.isActive() || !docStore) {
    return
  }
  const prompt = /** @type {any} */ (document.querySelector('#inpaint-prompt')).value.trim()
  if (!prompt) {
    return
  }
  const srcObj = inpaint.getSrcObj()
  try {
    await inpaint.submit(prompt)
    // 结果落在源图右侧(lineage 标记 inpaint);提交后退出重绘模式
    inpaint.exit()
    setInpaintUi(false)
    void srcObj
  } catch (err) {
    // 提示词为空/未涂抹:留在重绘模式
    ;/** @type {any} */ (document.querySelector('#inpaint-prompt')).setAttribute(
      'help-text',
      err instanceof Error ? err.message : String(err),
    )
  }
})

/* ---------- 以图生图(T9,线框 S9):参考图来自画布选中图 ---------- */

/** @type {string | null} 参考图对象 id */
let referenceObjId = null

const refChip = $('#ref-chip')
const refThumb = /** @type {HTMLImageElement} */ ($('#ref-thumb'))

function refObject() {
  const obj = docStore?.doc.objects[referenceObjId || '']
  return obj && (obj.kind === 'image' || obj.kind === 'video') ? obj : null
}

/** @param {string | null} objId */
function setReference(objId) {
  referenceObjId = objId
  const obj = docStore?.doc.objects[objId || '']
  if (obj && (obj.kind === 'image' || obj.kind === 'video')) {
    refThumb.src = obj.assetId ? `/assets/${obj.assetId}/t256.webp` : obj.src || ''
    refChip.hidden = false
    $('#ref-dropzone').hidden = true
  } else {
    referenceObjId = null
    refChip.hidden = true
    $('#ref-dropzone').hidden = false
  }
}

$('#ref-remove').addEventListener('click', () => setReference(null))

/** 服务端接受的参考图 MIME;其余格式(如 Mock 的 SVG)栅格化为 PNG */
const INIT_IMAGE_MIMES = new Set(['image/png', 'image/jpeg', 'image/webp'])

/** 位图化:任意可显示图 → PNG data URL @param {string} url */
function rasterizeToPng(url) {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => {
      const canvas = document.createElement('canvas')
      canvas.width = img.naturalWidth
      canvas.height = img.naturalHeight
      const ctx = canvas.getContext('2d')
      if (!ctx) {
        reject(new Error('参考图转换失败'))
        return
      }
      ctx.drawImage(img, 0, 0)
      resolve(canvas.toDataURL('image/png'))
    }
    img.onerror = () => reject(new Error('参考图转换失败'))
    img.src = url
  })
}

/** 对象原图转 dataURL(图生图 initImage) @param {any} obj */
async function objectToDataUrl(obj) {
  const url = obj.assetId ? `/assets/${obj.assetId}/original.${obj.ext || 'png'}` : obj.src
  if (!url) {
    throw new Error('参考图不可用')
  }
  const blob = await (await fetch(url)).blob()
  if (INIT_IMAGE_MIMES.has(blob.type)) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(String(reader.result))
      reader.onerror = () => reject(new Error('参考图读取失败'))
      reader.readAsDataURL(blob)
    })
  }
  return rasterizeToPng(url)
}

const contextMenu = initContextMenu({
  getDocStore: () => docStore,
  onUseAsReference: (/** @type {string} */ objId) => {
    setReference(objId)
    setPanelOpen(true)
  },
})

$('#prompt-generate').addEventListener('click', async () => {
  if (!docStore) {
    return
  }
  const prompt = /** @type {any} */ (promptInput).value.trim()
  if (!prompt) {
    ;/** @type {any} */ (promptInput).focus()
    promptInput.setAttribute('help-text', '请先输入提示词')
    return
  }
  const promptModel = /** @type {any} */ ($('#prompt-model'))
  const model = promptModel?.value || promptModel?.querySelector('sl-option')?.value || undefined
  const size = Number(/** @type {any} */ ($('#prompt-size'))?.value) || 512
  const seedRaw = Number(/** @type {any} */ ($('#prompt-seed'))?.value)
  const cam = engine.getCam()
  const seed = Number.isFinite(seedRaw) && seedRaw >= 0 ? Math.floor(seedRaw) : -1
  const genParams = {
    prompt,
    negativePrompt: '',
    model,
    width: size,
    height: size,
    steps: 20,
    cfgScale: 7,
    seed,
    sampler: 'euler',
    scheduler: 'normal',
    denoise: 1,
    kind: 'image',
    durationSec: 4,
    fps: 16,
  }
  let initImage
  let anchor = {
    x: cam.x + engine.stage.width() / cam.scale / 2 - size / 2,
    y: cam.y + engine.stage.height() / cam.scale / 2 - size / 2,
  }
  const ref = refObject()
  if (ref) {
    // 以图生图(PRD §5.3-②):锚点 = 源图右上;结果携带谱系;源图不动
    try {
      initImage = { dataUrl: await objectToDataUrl(ref) }
    } catch (err) {
      promptInput.setAttribute('help-text', err instanceof Error ? err.message : String(err))
      return
    }
    genParams.denoise = 0.75
    anchor = { x: ref.x + ref.width + 40, y: ref.y }
  }
  await submitGeneration({
    docStore,
    anchor,
    genParams,
    initImage,
    lineage: ref ? { fromId: ref.id, params: { ...genParams } } : undefined,
  })
  if (ref) {
    setReference(null)
  }
  // 生成后面板收起,画布不锁死可继续发任务(PRD §5.3-①)
  setPanelOpen(false)
})

export { engine, setPanelOpen, canvasEmpty, getDocStore, getObjectLayer, getGenerationEvents }
