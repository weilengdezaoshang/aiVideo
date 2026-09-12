// 对象右键菜单(线框 S8):图像对象的统一操作入口。
// 菜单项:以此为参考生成(回调)/ 局部重绘(T11)/ 生成视频(T12)/ 转矢量(T15,svg-export.ts)/ 下载 / 删除。

import { openSvgExportDialog } from '../svg-export.js'
import type { CanvasEngine } from '../engine/stage.js'
import type { CanvasDocData } from '../state/doc-store.js'
import type { CanvasObj } from '../state/commands.js'
import type { ObjectLayer } from '../engine/object-layer.js'

export type ContextMenuOptions = {
  getDocStore: () => { doc: Pick<CanvasDocData, 'objects'> } | null
  onOpenChange?: (open: boolean) => void
  onAddToChat?: (objId: string) => void
  onVariants?: (objId: string) => void
  onCutout?: (objId: string) => void
}

export type ContextMenu = {
  /** 绑定 Konva 舞台;由 app.ts 在对象层就绪后调用 */
  attach(
    engine: Pick<CanvasEngine, 'interaction' | 'stage' | 'contentLayer'>,
    getObjectLayer: () => Pick<ObjectLayer, 'setSelected' | 'deleteSelected'>,
  ): void
}

export function initContextMenu(options: ContextMenuOptions): ContextMenu {
  const menu = document.querySelector<HTMLElement>('#context-menu')!
  let targetObjId: string | null = null

  function openAt(x: number, y: number, objId: string) {
    targetObjId = objId
    menu.hidden = false
    options.onOpenChange?.(true)
    // 边界防溢出:贴边时向内收
    const rect = menu.getBoundingClientRect()
    const left = Math.min(x, window.innerWidth - rect.width - 8)
    const top = Math.min(y, window.innerHeight - rect.height - 8)
    menu.style.left = `${Math.max(8, left)}px`
    menu.style.top = `${Math.max(8, top)}px`
  }

  function close() {
    const wasOpen = !menu.hidden
    menu.hidden = true
    targetObjId = null
    if (wasOpen) {
      options.onOpenChange?.(false)
    }
  }

  /** @returns 菜单适用于该对象类型 */
  function applicable(obj: CanvasObj | undefined) {
    if (!obj) {
      return false
    }
    return (
      !(obj.nodeDraft && !obj.src && !obj.assetId) &&
      (obj.kind === 'image' || obj.kind === 'video' || obj.kind === 'error')
    )
  }

  return {
    attach(engine, getObjectLayer) {
      engine.interaction.subscribe((state) => {
        if (state.gesture || state.effective !== 'select') {
          close()
        }
      })
      // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Konva 以 UMD vendor 引入,无类型来源
      engine.stage.on('contextmenu', (e: any) => {
        e.evt.preventDefault()
        const state = engine.interaction.getState()
        if (state.gesture || state.effective !== 'select') {
          close()
          return
        }
        const doc = options.getDocStore()?.doc
        if (!doc) {
          return
        }
        // 用事件坐标(contextmenu 不更新 Konva 内部指针位置)
        const rect = engine.stage.container().getBoundingClientRect()
        const pos = { x: e.evt.clientX - rect.left, y: e.evt.clientY - rect.top }
        const hit = engine.contentLayer.getIntersection(pos)
        let objId: string | null = null
        if (hit) {
          // 命中的可能是子节点,向上找文档对象 id
          // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Konva 以 UMD vendor 引入,无类型来源
          let node: any = hit
          while (node && !doc.objects[node.id()]) {
            node = node.parent
          }
          objId = node ? node.id() : null
        }
        if (objId && applicable(doc.objects[objId])) {
          getObjectLayer().setSelected([objId])
          openAt(e.evt.clientX, e.evt.clientY, objId)
        } else {
          close()
        }
      })

      // 点击别处 / Esc 关闭
      window.addEventListener('mousedown', (e) => {
        if (!menu.hidden && !menu.contains(e.target as Node)) {
          close()
        }
      })
      window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
          close()
        }
      })

      // 菜单项
      document.querySelector('#ctx-add-chat')?.addEventListener('click', () => {
        if (targetObjId) {
          options.onAddToChat?.(targetObjId)
        }
        close()
      })
      document.querySelector('#ctx-variants')?.addEventListener('click', () => {
        const obj = targetObjId ? options.getDocStore()?.doc.objects[targetObjId] : null
        if (obj && obj.kind === 'image' && targetObjId) {
          options.onVariants?.(targetObjId)
        }
        close()
      })
      document.querySelector('#ctx-cutout')?.addEventListener('click', () => {
        const obj = targetObjId ? options.getDocStore()?.doc.objects[targetObjId] : null
        if (obj && obj.kind === 'image' && targetObjId) {
          options.onCutout?.(targetObjId)
        }
        close()
      })
      document.querySelector('#ctx-vector')?.addEventListener('click', () => {
        const obj = targetObjId ? options.getDocStore()?.doc.objects[targetObjId] : null
        const url = obj?.assetId ? `/assets/${obj.assetId}/original.${obj.ext || 'png'}` : obj?.src
        if (url) {
          void openSvgExportDialog({ imageUrl: url, filename: `gencanvas-${targetObjId}.svg` })
        }
        close()
      })
      document.querySelector('#ctx-download')?.addEventListener('click', () => {
        const obj = targetObjId ? options.getDocStore()?.doc.objects[targetObjId] : null
        if (obj) {
          void downloadObject(obj)
        }
        close()
      })
      document.querySelector('#ctx-delete')?.addEventListener('click', () => {
        if (targetObjId) {
          getObjectLayer().deleteSelected()
        }
        close()
      })
    },
  }
}

/** 下载对象原图(fetch → blob,保留服务端文件名语义) */
export async function downloadObject(obj: { src?: string; assetId?: string; ext?: string }) {
  const url = obj.assetId ? `/assets/${obj.assetId}/original.${obj.ext || 'png'}` : obj.src
  if (!url) {
    return
  }
  const res = await fetch(url)
  const blob = await res.blob()
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `gencanvas-${Date.now()}.${obj.ext || 'png'}`
  a.click()
  URL.revokeObjectURL(a.href)
}
