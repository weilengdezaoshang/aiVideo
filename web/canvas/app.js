// GenCanvas 画布入口 —— T1 仅静态骨架交互(Tab 呼出/收起面板、缩放显示位);
// 画布引擎(T4)、状态层(T5)、生成闭环(T8)按 docs/开发计划-GenCanvas.md 逐步落地。

const $ = (sel) => document.querySelector(sel)

const promptPanel = $('#prompt-panel')
const promptInput = $('#prompt-input')
const canvasEmpty = $('#canvas-empty')

/** 提示词面板展开/收起;生成后面板自动收起(线框 S4 → S5)。 */
function setPanelOpen(open) {
  promptPanel.hidden = !open
  if (open) {
    canvasEmpty.hidden = false
    // Shoelace textarea 的聚焦走原生 input 元素
    const input = promptInput?.shadowRoot?.querySelector('textarea') ?? promptInput
    input?.focus()
  }
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Tab') {
    e.preventDefault()
    setPanelOpen(promptPanel.hidden)
  }
  if (e.key === 'Escape' && !promptPanel.hidden) {
    setPanelOpen(false)
  }
})

$('#prompt-collapse').addEventListener('click', () => setPanelOpen(false))

// 占位:缩放控件仅维护百分比显示,真实相机在 T4 由 Konva 驱动
let zoomPercent = 100
const zoomValue = $('#zoom-value')
const clampZoom = (z) => Math.min(500, Math.max(10, z))
const applyZoom = (z) => {
  zoomPercent = clampZoom(Math.round(z))
  zoomValue.textContent = `${zoomPercent}%`
}
$('#zoom-in').addEventListener('click', () => applyZoom(zoomPercent * 1.2))
$('#zoom-out').addEventListener('click', () => applyZoom(zoomPercent / 1.2))
