// GenCanvas 画布入口(ESM):组装引擎、文档状态层、自动保存、提示词面板。
// 对象渲染(T6)、SSE 对账(T7)、生成闭环(T8)按 docs/开发计划-GenCanvas.md 逐步落地。

import { createCanvasEngine } from './engine/stage.js'
import { createDocStore } from './state/doc-store.js'
import { createAutoSaver } from './state/autosave.js'
import { openDocument } from './state/document-api.js'
import { createObjectLayer } from './engine/object-layer.js'
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

initSettings()

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
export { engine, setPanelOpen, canvasEmpty, getDocStore, getObjectLayer }
