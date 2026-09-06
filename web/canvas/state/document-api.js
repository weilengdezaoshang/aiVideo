// 文档 API 封装:打开/创建画布文档,docId 记在 localStorage。
// 单标签页是 v1 约束(PRD §9);多开检测在 T14 接 BroadcastChannel。

const DOC_ID_KEY = 'gencanvas.docId'

/** @typedef {import('./commands.js').CanvasObj} CanvasObj */
/** @typedef {{ id: string, name: string, objects: Record<string, CanvasObj>, order: string[] }} CanvasDocData */

/** @param {string} path @param {RequestInit} [options] */
async function api(path, options) {
  const res = await fetch(path, options)
  const body = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new Error(body.error || `请求失败 HTTP ${res.status}`)
  }
  return body
}

/**
 * 打开应显示的文档:localStorage 记录的 → 服务端最近更新的 → 新建。
 * @returns {Promise<CanvasDocData>}
 */
export async function openDocument() {
  const remembered = localStorage.getItem(DOC_ID_KEY)
  if (remembered) {
    try {
      const { document } = await api(`/api/documents/${remembered}`)
      return document
    } catch {
      // 记录的文档已不存在(如 data 清理):走后面的取最近/新建
    }
  }
  const { documents } = await api('/api/documents')
  if (documents.length > 0) {
    localStorage.setItem(DOC_ID_KEY, documents[0].id)
    const { document } = await api(`/api/documents/${documents[0].id}`)
    return document
  }
  const { document } = await api('/api/documents', { method: 'POST' })
  localStorage.setItem(DOC_ID_KEY, document.id)
  return document
}

/** 新建空文档并切换(顶栏「新建画布」) @returns {Promise<CanvasDocData>} */
export async function createDocument() {
  const { document } = await api('/api/documents', { method: 'POST' })
  localStorage.setItem(DOC_ID_KEY, document.id)
  return document
}
