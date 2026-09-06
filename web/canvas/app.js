// GenCanvas 画布入口(ESM):组装引擎、顶栏、提示词面板。
// 状态层(T5)、对象渲染(T6)、生成闭环(T8)按 docs/开发计划-GenCanvas.md 逐步落地。

import { createCanvasEngine } from './engine/stage.js'

const $ = (sel) => document.querySelector(sel)

const rootEl = $('#canvas-root')
const surfaceEl = $('#canvas-surface')
const promptPanel = $('#prompt-panel')
const promptInput = $('#prompt-input')
const canvasEmpty = $('#canvas-empty')
const zoomValue = $('#zoom-value')

const engine = createCanvasEngine(rootEl, surfaceEl, {
  onCamMove: (cam) => {
    zoomValue.textContent = `${Math.round(cam.scale * 100)}%`
  },
})
// 初始视野:世界原点居中
engine.centerOn({ x: 0, y: 0 }, 1)

// 缩放控件:以视口中心为锚
const viewportCenter = () => ({ x: engine.stage.width() / 2, y: engine.stage.height() / 2 })
$('#zoom-in').addEventListener('click', () => engine.zoomBy(1.2, viewportCenter()))
$('#zoom-out').addEventListener('click', () => engine.zoomBy(1 / 1.2, viewportCenter()))

/* ---------- 提示词面板:Tab 呼出 / Esc 收起(线框 S3→S4) ---------- */

const isEditable = (el) =>
  el instanceof HTMLElement &&
  (el.tagName === 'INPUT' ||
    el.tagName === 'TEXTAREA' ||
    el.tagName === 'SL-INPUT' ||
    el.tagName === 'SL-TEXTAREA' ||
    el.isContentEditable ||
    el.closest?.('sl-input, sl-textarea, [contenteditable]') != null)

function setPanelOpen(open) {
  promptPanel.hidden = !open
  if (open) {
    // Shoelace textarea 的聚焦走内部原生元素
    const input = promptInput?.shadowRoot?.querySelector('textarea') ?? promptInput
    queueMicrotask(() => input?.focus())
  }
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Tab' && !isEditable(e.target)) {
    e.preventDefault()
    setPanelOpen(promptPanel.hidden)
  }
  if (e.key === 'Escape' && !promptPanel.hidden && !isEditable(e.target)) {
    setPanelOpen(false)
  }
})

$('#prompt-collapse').addEventListener('click', () => setPanelOpen(false))

// 空态引导只在画布无对象时显示;对象层变化后由渲染层隐藏(T6 接线)
export { engine, setPanelOpen, canvasEmpty }
