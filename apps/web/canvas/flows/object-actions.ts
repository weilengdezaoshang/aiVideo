// 选中对象操作栏(PRD §4.1/§4.2):单图贴图下方,能力为
// 「添加到对话 / 抠图 / 下载」;视频保留下载;文本/画板不显示图片专属按钮。
// 下载返回资产原文件(透明结果固定 PNG),反馈"已开始下载"(不宣称已保存磁盘)。

import { transformBounds } from '../engine/transform-geometry.js'
import { downloadUrl, objectOriginalUrl } from './asset-util.js'
import type { CanvasEngine } from '../engine/stage.js'
import type { CanvasDocData } from '../state/doc-store.js'
import type { CanvasObj } from '../state/commands.js'

export type ObjectActionsOptions = {
  engine: Pick<CanvasEngine, 'interaction' | 'getCam' | 'worldToScreen' | 'panBy'>
  rootEl: HTMLElement
  getDocStore: () => { doc: Pick<CanvasDocData, 'objects'> } | null
  getSelection: () => string[]
  onAddToChat: (objId: string) => void
  onCutout: (objId: string) => void
  toast?: (text: string) => void
}

export type ObjectActions = {
  refresh(opts?: { pan?: boolean }): void
  hide(): void
  setContextMenuOpen(open: boolean): void
}

export function initObjectActions({
  engine,
  rootEl,
  getDocStore,
  getSelection,
  onAddToChat,
  onCutout,
  toast = () => {},
}: ObjectActionsOptions): ObjectActions {
  const bar = document.querySelector<HTMLElement>('#obj-actions')!
  const addChatBtn = document.querySelector<HTMLButtonElement>('#obj-add-chat')!
  const cutoutBtn = document.querySelector<HTMLButtonElement>('#obj-cutout')!
  const downloadBtn = document.querySelector<HTMLButtonElement>('#obj-download')!

  const cutoutBar = document.querySelector<HTMLElement>('#cutout-bar')!
  const inpaintBar = document.querySelector<HTMLElement>('#inpaint-bar')!
  let contextMenuOpen = false

  document.addEventListener('gencanvas:maskmode', () => refresh())

  function isImageLike(obj: CanvasObj | undefined) {
    return obj && obj.kind === 'image'
  }

  /** 根据选中对象刷新按钮可见性与位置。pan:选择变化时允许轻移视口腾出空间(PRD §4.1) */
  function refresh(opts: { pan?: boolean } = {}) {
    const selection = getSelection()
    const editingMask = !cutoutBar.hidden
    const single = editingMask
      ? getDocStore()?.doc.objects[cutoutBar.dataset.sourceId || '']
      : selection.length === 1
        ? getDocStore()?.doc.objects[selection[0]]
        : null
    if (
      contextMenuOpen ||
      (!editingMask && engine.interaction && !engine.interaction.canShowObjectActions()) ||
      !single ||
      single.kind === 'placeholder' ||
      (single.nodeDraft && !single.src && !single.assetId) ||
      single.textCard ||
      !inpaintBar.hidden
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
    const bounds = transformBounds(single)
    const screen = engine.worldToScreen(bounds)
    const bottom = screen.y + bounds.height * cam.scale + 10
    const activeBar = editingMask ? cutoutBar : bar
    activeBar.hidden = false
    const width = activeBar.offsetWidth || 240
    const height = activeBar.offsetHeight || 48
    // 蒙版工具较多，贴近左侧时避开悬浮工具条。
    const leftInset = editingMask ? Math.min(80, Math.max(8, rootEl.clientWidth - width - 8)) : 8
    const x = Math.min(
      Math.max(leftInset, screen.x + (bounds.width * cam.scale - width) / 2),
      Math.max(8, rootEl.clientWidth - width - 8),
    )
    let y = Math.min(bottom, Math.max(8, rootEl.clientHeight - height))
    if (opts.pan && bottom + height > rootEl.clientHeight) {
      // 下方不足:轻移视口把对象底边抬进安全区,操作栏保持贴图下方
      const overflow = bottom + height - rootEl.clientHeight
      engine.panBy(0, overflow + 12)
      y = rootEl.clientHeight - height - 8
    }
    activeBar.style.left = `${x}px`
    activeBar.style.top = `${y}px`
    bar.hidden = editingMask
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

  return {
    refresh,
    hide,
    setContextMenuOpen(open: boolean) {
      contextMenuOpen = open
      refresh()
    },
  }
}
