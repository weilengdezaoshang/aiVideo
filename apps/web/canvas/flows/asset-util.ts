import type { CanvasObj } from '../state/commands.js'
// 资产工具:画布对象 → 服务端资产(cutout / agent 共用)。
// 无 assetId 的对象(历史图片/拖入图)先上传入库,拿到稳定 assetId 再参与后续流程。

/** 服务端接受的参考图 MIME;其余格式栅格化为 PNG */
const INIT_IMAGE_MIMES = new Set(['image/png', 'image/jpeg', 'image/webp'])

/** @param {string} url @returns {Promise<string>} PNG data URL */
export function rasterizeToPng(url: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => {
      const canvas = document.createElement('canvas')
      canvas.width = img.naturalWidth
      canvas.height = img.naturalHeight
      const ctx = canvas.getContext('2d')
      if (!ctx) {
        reject(new Error('图片转换失败'))
        return
      }
      ctx.drawImage(img, 0, 0)
      resolve(canvas.toDataURL('image/png'))
    }
    img.onerror = () => reject(new Error('图片转换失败'))
    img.src = url
  })
}

/** 对象原图转 dataURL(图生图 initImage)。 @param {any} obj */
export async function objectToDataUrl(obj: Partial<CanvasObj>): Promise<string> {
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

/**
 * 确保对象有 assetId:没有则下载原图上传入库。
 * @param {any} obj
 * @returns {Promise<{ id: string, ext: string }>}
 */
export async function ensureAsset(
  obj: Partial<CanvasObj>,
  signal?: AbortSignal,
): Promise<{ id: string; ext: string }> {
  if (obj.assetId) {
    return { id: obj.assetId, ext: obj.ext || 'png' }
  }
  if (!obj.src) {
    throw new Error('原图资源不可用')
  }
  const source = await fetch(obj.src, { signal })
  if (!source.ok) {
    throw new Error('原图下载失败')
  }
  const blob = await source.blob()
  const upload = await fetch(`/api/assets?ext=${obj.ext || 'png'}&kind=image`, {
    method: 'POST',
    signal,
    headers: { 'Content-Type': blob.type || 'image/png' },
    body: blob,
  })
  const body = await upload.json()
  if (!upload.ok) {
    throw new Error(body.error || '原图入库失败')
  }
  return { id: body.asset.id, ext: body.asset.ext }
}

/** 对象缩略图 URL(对话卡展示用)。 @param {any} obj */
export function objectThumbUrl(obj: Partial<CanvasObj>) {
  if (!obj) {
    return ''
  }
  if (obj.assetId) {
    return obj.hasThumbs === false
      ? `/assets/${obj.assetId}/original.${obj.ext || 'png'}`
      : `/assets/${obj.assetId}/t256.webp`
  }
  return obj.src || ''
}

/** 对象原图 URL(下载用)。 @param {any} obj */
export function objectOriginalUrl(obj: Partial<CanvasObj>) {
  if (!obj) {
    return ''
  }
  return obj.assetId ? `/assets/${obj.assetId}/original.${obj.ext || 'png'}` : obj.src || ''
}

/** 触发浏览器下载原文件;返回 false 表示浏览器拦截了下载。 @param {string} url @param {string} filename */
export function downloadUrl(url: string, filename: string) {
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.append(anchor)
  const ok = anchor.click instanceof Function
  anchor.click()
  anchor.remove()
  return ok
}
