// 对象右键菜单(线框 S8):图像对象的统一操作入口。
// 菜单项:以此为参考生成(回调)/ 局部重绘(T11)/ 生成视频(T12)/ 转矢量(T15,svg-export.js)/ 下载 / 删除。

import { openSvgExportDialog } from '../svg-export.js'

/**
 * @param {{
 *   getDocStore: () => any,
 *   onAddToChat?: (objId: string) => void,
 *   onVariants?: (objId: string) => void,
 *   onCutout?: (objId: string) => void,
 * }} options
 */
export function initContextMenu(options) {
  const menu = /** @type {HTMLElement} */ (document.querySelector('#context-menu'))
  /** @type {string | null} */
  let targetObjId = null

  /** @param {number} x @param {number} y @param {string} objId */
  function openAt(x, y, objId) {
    targetObjId = objId
    menu.hidden = false
    // 边界防溢出:贴边时向内收
    const rect = menu.getBoundingClientRect()
    const left = Math.min(x, window.innerWidth - rect.width - 8)
    const top = Math.min(y, window.innerHeight - rect.height - 8)
    menu.style.left = `${Math.max(8, left)}px`
    menu.style.top = `${Math.max(8, top)}px`
  }

  function close() {
    menu.hidden = true
    targetObjId = null
  }

  /** @param {any} obj @returns {boolean} 菜单适用于该对象类型 */
  function applicable(obj) {
    return Boolean(obj) && (obj.kind === 'image' || obj.kind === 'video' || obj.kind === 'error')
  }

  return {
    /** 绑定 Konva 舞台;由 app.js 在对象层就绪后调用 @param {any} engine @param {() => any} getObjectLayer */
    attach(engine, getObjectLayer) {
      engine.stage.on('contextmenu', (/** @type {any} */ e) => {
        e.evt.preventDefault()
        const doc = options.getDocStore()?.doc
        if (!doc) {
          return
        }
        // 用事件坐标(contextmenu 不更新 Konva 内部指针位置)
        const rect = engine.stage.container().getBoundingClientRect()
        const pos = { x: e.evt.clientX - rect.left, y: e.evt.clientY - rect.top }
        const hit = engine.contentLayer.getIntersection(pos)
        let objId = null
        if (hit) {
          // 命中的可能是子节点,向上找文档对象 id
          let node = hit
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
        if (!menu.hidden && !menu.contains(/** @type {Node} */ (e.target))) {
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

/** 下载对象原图(fetch → blob,保留服务端文件名语义) @param {{ src?: string, assetId?: string, ext?: string }} obj */
export async function downloadObject(obj) {
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
