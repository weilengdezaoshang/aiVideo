// 选中对象操作栏(PRD §4.1/§4.2):单图贴图下方,能力为
// 「添加到对话 / 抠图 / 下载」;视频保留下载;文本/画板不显示图片专属按钮。
// 下载返回资产原文件(透明结果固定 PNG),反馈"已开始下载"(不宣称已保存磁盘)。

import { downloadUrl, objectOriginalUrl } from './asset-util.js'

/**
 * @param {{
 *   engine: any,
 *   rootEl: HTMLElement,
 *   getDocStore: () => any,
 *   getSelection: () => string[],
 *   onAddToChat: (objId: string) => void,
 *   onCutout: (objId: string) => void,
 *   toast?: (text: string) => void,
 * }} options
 */
export function initObjectActions({
  engine,
  rootEl,
  getDocStore,
  getSelection,
  onAddToChat,
  onCutout,
  toast = () => {},
}) {
  const bar = /** @type {HTMLElement} */ (document.querySelector('#obj-actions'))
  const addChatBtn = /** @type {HTMLButtonElement} */ (document.querySelector('#obj-add-chat'))
  const cutoutBtn = /** @type {HTMLButtonElement} */ (document.querySelector('#obj-cutout'))
  const downloadBtn = /** @type {HTMLButtonElement} */ (document.querySelector('#obj-download'))

  /** @param {any} obj */
  function isImageLike(obj) {
    return obj && obj.kind === 'image'
  }

  /** 根据选中对象刷新按钮可见性与位置。 */
  function refresh() {
    const selection = getSelection()
    const single = selection.length === 1 ? getDocStore()?.doc.objects[selection[0]] : null
    if (
      !single ||
      single.kind === 'placeholder' ||
      // 蒙版 / 重绘栏显示期间不呈现操作栏(避免浮层叠浮层)
      !/** @type {HTMLElement} */ (document.querySelector('#cutout-bar')).hidden ||
      !/** @type {HTMLElement} */ (document.querySelector('#inpaint-bar')).hidden
    ) {
      bar.hidden = true
      return
    }
    const hasFile = single.kind === 'image' || single.kind === 'video'
    addChatBtn.hidden = !isImageLike(single)
    cutoutBtn.hidden = !isImageLike(single)
    downloadBtn.hidden = !hasFile // 画板/文字没有可下载的原文件(PRD §8)
    if (downloadBtn.hidden && addChatBtn.hidden && cutoutBtn.hidden) {
      bar.hidden = true
      return
    }
    // 位置:贴对象下方,左右夹紧画布安全边界(PRD §4.1)
    const cam = engine.getCam()
    const screen = engine.worldToScreen({ x: single.x, y: single.y })
    const bottom = screen.y + single.height * cam.scale + 10
    const width = bar.offsetWidth || 240
    const x = Math.min(
      Math.max(8, screen.x + (single.width * cam.scale - width) / 2),
      Math.max(8, rootEl.clientWidth - width - 8),
    )
    const y = Math.min(bottom, Math.max(8, rootEl.clientHeight - 48))
    bar.style.left = `${x}px`
    bar.style.top = `${y}px`
    bar.hidden = false
  }

  function hide() {
    bar.hidden = true
  }

  addChatBtn.addEventListener('click', () => {
    const [objId] = getSelection()
    if (objId) {
      onAddToChat(objId)
    }
  })
  cutoutBtn.addEventListener('click', () => {
    const [objId] = getSelection()
    if (objId) {
      onCutout(objId)
    }
  })
  downloadBtn.addEventListener('click', () => {
    const [objId] = getSelection()
    const obj = objId ? getDocStore()?.doc.objects[objId] : null
    if (!obj || !obj.kind || obj.kind === 'placeholder' || obj.kind === 'error') {
      return
    }
    const url = objectOriginalUrl(obj)
    if (!url) {
      return
    }
    const ext = obj.ext || 'png'
    downloadUrl(url, `gencanvas-${objId.slice(-6)}.${ext}`)
    toast('已开始下载')
  })

  return { refresh, hide }
}
