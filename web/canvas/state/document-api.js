// 文档 API 封装:打开/创建画布文档。 
// 稳定地址(PRD §3):/canvas?doc=<id> 严格打开指定文档;不存在就是不存在,
// 不用新建文档兜底;读取失败与不存在分开提示。 
// 无 doc 参数时保持旧行为(localStorage → 最近 → 新建),兼容旧入口。 
// 单标签页是 v1 约束(PRD §9);多开检测在 duplicate-tabs.js。 

const DOC_ID_KEY = 'gencanvas.docId'
const DRAFT_PREFIX = 'gencanvas.docDraft.'

/**
 * 本机草稿(PRD §9/A13):自动保存前先把快照落 localStorage,作为服务端不可用时的安全网;
 * 与主题偏好一样属于本机存储,与文档服务端存储分离。
 * @param {string} docId @param {any} snapshot @returns {boolean} 是否写入成功
 */
export function writeLocalDraft(docId, snapshot) {
  try {
    localStorage.setItem(
      DRAFT_PREFIX + docId,
      JSON.stringify({ at: new Date().toISOString(), snapshot }),
    )
    return true
  } catch {
    return false
  }
}

/** @param {string} docId @returns {any | null} 本机草稿快照(无则 null) */
export function loadLocalDraft(docId) {
  try {
    const raw = localStorage.getItem(DRAFT_PREFIX + docId)
    if (!raw) {
      return null
    }
    const parsed = JSON.parse(raw)
    return parsed?.snapshot && typeof parsed.snapshot === 'object' ? parsed.snapshot : null
  } catch {
    return null
  }
}

/** @param {string} docId */
export function clearLocalDraft(docId) {
  try {
    localStorage.removeItem(DRAFT_PREFIX + docId)
  } catch {
    // 清理失败无碍:草稿只作为兜底
  }
}

/** @typedef {{ missing?: boolean, loadError?: boolean }} OpenFailure */
/** 文档不存在(404):调用方显示"文档不存在",绝不自动新建 */
export class DocumentMissingError extends Error {
  /** @param {string} docId */
  constructor(docId) {
    super(`文档不存在:${docId}`)
    this.name = 'DocumentMissingError'
    this.missing = true
  }
}

/** @typedef {import('./commands.js').CanvasObj} CanvasObj */
/** @typedef {{ id: string, name: string, objects: Record<string, CanvasObj>, order: string[], revision: number, groups?: Record<string, any>, chat?: any[] }} CanvasDocData */

/** @param {string} path @param {RequestInit} [options] @returns {Promise<any>} */
async function api(path, options) {
  const res = await fetch(path, options)
  const body = await res.json().catch(() => ({}))
  if (!res.ok) {
    const err = new Error(body.error || `请求失败 HTTP ${res.status}`)
    ;/** @type {any} */ (err).status = res.status
    throw err
  }
  return body
}

/**
 * 打开应显示的文档:
 *   ?doc=<id> → 严格打开(404 抛 DocumentMissingError,其他错误原样上抛)
 *   无参数    → localStorage 记录的 → 服务端最近更新的 → 新建
 * @returns {Promise<CanvasDocData>}
 */
export async function openDocument() {
  const docId = new URLSearchParams(location.search).get('doc')
  if (docId) {
    try {
      const { document: doc } = await api(`/api/documents/${encodeURIComponent(docId)}`)
      localStorage.setItem(DOC_ID_KEY, doc.id)
      return doc
    } catch (err) {
      if (err instanceof Error && /** @type {any} */ (err).status === 404) {
        throw new DocumentMissingError(docId)
      }
      throw err
    }
  }
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
  return createDocument()
}

/**
 * 新建文档(幂等,PRD §3 A01):同一 idempotencyKey 服务端只产出一份;
 * 网络超时后用同一 key 重查,不会新建第二份。 
 * @param {{ name?: string, idempotencyKey?: string }} [options]
 * @returns {Promise<CanvasDocData>}
 */
export async function createDocument(options = {}) {
  const key = options.idempotencyKey || `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
  const { document } = await api('/api/documents', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
    body: JSON.stringify(options.name ? { name: options.name } : {}),
  })
  localStorage.setItem(DOC_ID_KEY, document.id)
  return document
}

/** 重命名(只动名称,不与内容快照互相覆盖)。 @param {string} docId @param {string} name */
export async function renameDocument(docId, name) {
  const { document } = await api(`/api/documents/${docId}/rename`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
  return document
}
