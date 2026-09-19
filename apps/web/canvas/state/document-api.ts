// 文档 API 封装:打开/创建画布文档。
// 稳定地址(PRD §3):/canvas?doc=<id> 严格打开指定文档;不存在就是不存在,
// 不用新建文档兜底;读取失败与不存在分开提示。
// 无 doc 参数时保持旧行为(localStorage → 最近 → 新建),兼容旧入口。
// 单标签页是 v1 约束(PRD §9);多开检测在 duplicate-tabs.js。

import type { CanvasDocData } from './doc-store.js'

const DOC_ID_KEY = 'gencanvas.docId'
const DRAFT_PREFIX = 'gencanvas.docDraft.'

/**
 * 本机草稿(PRD §9/A13):自动保存前先把快照落 localStorage,作为服务端不可用时的安全网;
 * 与主题偏好一样属于本机存储,与文档服务端存储分离。
 * @returns 是否写入成功
 */
export function writeLocalDraft(docId: string, snapshot: unknown): boolean {
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

/** @returns 本机草稿快照(无则 null) */
export function loadLocalDraft(docId: string): CanvasDocData | null {
  try {
    const raw = localStorage.getItem(DRAFT_PREFIX + docId)
    if (!raw) {
      return null
    }
    const parsed = JSON.parse(raw) as { snapshot?: CanvasDocData }
    return parsed?.snapshot && typeof parsed.snapshot === 'object' ? parsed.snapshot : null
  } catch {
    return null
  }
}

export function clearLocalDraft(docId: string): void {
  try {
    localStorage.removeItem(DRAFT_PREFIX + docId)
  } catch {
    // 清理失败无碍:草稿只作为兜底
  }
}

/** 草稿与服务端文档的内容是否一致(忽略 revision/updatedAt 等元字段)。 */
export function draftMatchesDocument(draft: CanvasDocData, doc: CanvasDocData): boolean {
  const content = (d: CanvasDocData) =>
    JSON.stringify({
      name: d.name,
      objects: d.objects,
      order: d.order,
      groups: d.groups,
      chat: d.chat,
      storyboard: d.storyboard ?? null,
      timeline: d.timeline ?? null,
    })
  return content(draft) === content(doc)
}

/**
 * 打开文档成功后对本机草稿的处置决定(已知问题 #2 回归):
 * - 无草稿:直接打开服务端文档;
 * - 草稿与服务端内容一致:服务端已包含草稿内容(等价保存确认),可清除兜底;
 * - 不一致:草稿可能携带未保存修改(保存失败/冲突/beacon 未送达),必须保留,
 *   由用户显式选择恢复或放弃,不得静默丢弃。
 */
export type DraftResolution =
  { action: 'open-server'; clearDraft: boolean } | { action: 'needs-choice'; draft: CanvasDocData }

export function resolveDraftOnOpen(
  doc: CanvasDocData,
  draft: CanvasDocData | null,
): DraftResolution {
  if (!draft) {
    return { action: 'open-server', clearDraft: false }
  }
  if (draftMatchesDocument(draft, doc)) {
    return { action: 'open-server', clearDraft: true }
  }
  return { action: 'needs-choice', draft }
}

/** 文档不存在(404):调用方显示"文档不存在",绝不自动新建 */
export class DocumentMissingError extends Error {
  missing = true
  constructor(docId: string) {
    super(`文档不存在:${docId}`)
    this.name = 'DocumentMissingError'
  }
}

type ErrorWithStatus = Error & { status?: number }

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(path, options)
  const body = (await res.json().catch(() => ({}))) as T & { error?: string }
  if (!res.ok) {
    const err: ErrorWithStatus = new Error(body.error || `请求失败 HTTP ${res.status}`)
    err.status = res.status
    throw err
  }
  return body
}

/**
 * 打开应显示的文档:
 *   ?doc=<id> → 严格打开(404 抛 DocumentMissingError,其他错误原样上抛)
 *   无参数    → localStorage 记录的 → 服务端最近更新的 → 新建
 */
export async function openDocument(): Promise<CanvasDocData> {
  const docId = new URLSearchParams(location.search).get('doc')
  if (docId) {
    try {
      const { document: doc } = await api<{ document: CanvasDocData }>(
        `/api/documents/${encodeURIComponent(docId)}`,
      )
      localStorage.setItem(DOC_ID_KEY, doc.id)
      return doc
    } catch (err) {
      if (err instanceof Error && (err as ErrorWithStatus).status === 404) {
        throw new DocumentMissingError(docId)
      }
      throw err
    }
  }
  const remembered = localStorage.getItem(DOC_ID_KEY)
  if (remembered) {
    try {
      const { document } = await api<{ document: CanvasDocData }>(`/api/documents/${remembered}`)
      return document
    } catch {
      // 记录的文档已不存在(如 data 清理):走后面的取最近/新建
    }
  }
  const { documents } = await api<{ documents: { id: string }[] }>('/api/documents')
  if (documents.length > 0) {
    localStorage.setItem(DOC_ID_KEY, documents[0].id)
    const { document } = await api<{ document: CanvasDocData }>(`/api/documents/${documents[0].id}`)
    return document
  }
  return createDocument()
}

/**
 * 新建文档(幂等,PRD §3 A01):同一 idempotencyKey 服务端只产出一份;
 * 网络超时后用同一 key 重查,不会新建第二份。
 */
export async function createDocument(
  options: { name?: string; idempotencyKey?: string } = {},
): Promise<CanvasDocData> {
  const key =
    options.idempotencyKey ||
    `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
  const { document } = await api<{ document: CanvasDocData }>('/api/documents', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
    body: JSON.stringify(options.name ? { name: options.name } : {}),
  })
  localStorage.setItem(DOC_ID_KEY, document.id)
  return document
}

/** 重命名(只动名称,不与内容快照互相覆盖)。 */
export async function renameDocument(docId: string, name: string): Promise<CanvasDocData> {
  const { document } = await api<{ document: CanvasDocData }>(`/api/documents/${docId}/rename`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
  return document
}
