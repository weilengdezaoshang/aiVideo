import { promises as fs } from 'node:fs'
import path from 'node:path'
import { randomUUID } from 'node:crypto'
import { logger } from './logger.js'

/**
 * 画布文档(PRD §7.1):对象与谱系;服务端只守卫形状与大小,内容语义归前端。
 * revision 是乐观并发版本号(PRD §9):每次保存 +1;客户端带 baseRevision 保存时
 * 与服务端不一致即冲突,由上层转 409,不用最后写入静默覆盖。
 */
export interface CanvasDoc {
  id: string
  name: string
  /** id → 对象(图像/视频/占位/错误卡);只存 assetId 引用不存 blob */
  objects: Record<string, unknown>
  /** 渲染顺序(对象 id) */
  order: string[]
  /** 四方向组元数据(PRD §5):groupId → { 标题、原始需求、方向列表 } */
  groups?: Record<string, unknown>
  /** 对话消息(PRD §7):随文档持久化,刷新后恢复会话 */
  chat?: unknown[]
  revision: number
  updatedAt: string
}

export interface DocumentSummary {
  id: string
  name: string
  updatedAt: string
  objectCount: number
}

/** 保存冲突:携带服务端当前版本,路由层转 409 让客户端决定合并策略。 */
export class DocumentConflictError extends Error {
  constructor(
    message: string,
    public readonly serverDoc: CanvasDoc,
  ) {
    super(message)
    this.name = 'DocumentConflictError'
  }
}

const DOC_ID_RE = /^[a-f0-9-]{36}$/
/** 文档序列化上限:5000 对象的引用 + 谱系参数绰绰有余,防异常 payload 撑爆磁盘 */
const MAX_DOC_BYTES = 5 * 1024 * 1024
const MAX_OBJECTS = 5000
const MAX_CHAT_MESSAGES = 500
const MAX_GROUPS = 200

export class DocumentStore {
  constructor(private dir: string) {}

  async init(): Promise<void> {
    await fs.mkdir(this.dir, { recursive: true })
  }

  private file(id: string): string | undefined {
    return DOC_ID_RE.test(id) ? path.join(this.dir, `${id}.json`) : undefined
  }

  /** 幂等创建索引(PRD §3 A01):key → docId,防"连续点击/超时重试"产出第二份文档 */
  private idempotencyFile(): string {
    return path.join(this.dir, '_idempotency.json')
  }

  private async readIdempotency(): Promise<Record<string, { docId: string; createdAt: string }>> {
    try {
      const raw = JSON.parse(await fs.readFile(this.idempotencyFile(), 'utf8')) as Record<
        string,
        { docId: string; createdAt: string }
      >
      return raw && typeof raw === 'object' ? raw : {}
    } catch {
      return {}
    }
  }

  private async writeIdempotency(map: Record<string, { docId: string; createdAt: string }>) {
    // 只保留最近 500 条,防止索引无限膨胀
    const entries = Object.entries(map).sort((a, b) => (a[1].createdAt < b[1].createdAt ? 1 : -1))
    const trimmed = Object.fromEntries(entries.slice(0, 500))
    const tmp = `${this.idempotencyFile()}.tmp`
    await fs.writeFile(tmp, JSON.stringify(trimmed))
    await fs.rename(tmp, this.idempotencyFile())
  }

  async create(name?: unknown, idempotencyKey?: string): Promise<CanvasDoc> {
    if (idempotencyKey) {
      const map = await this.readIdempotency()
      const hit = map[idempotencyKey]
      if (hit) {
        const existing = await this.get(hit.docId)
        if (existing) {
          logger.debug('幂等创建命中', { key: idempotencyKey, docId: existing.id })
          return existing
        }
      }
    }
    const now = new Date().toISOString()
    const doc: CanvasDoc = {
      id: randomUUID(),
      name: typeof name === 'string' && name.trim() ? name.trim().slice(0, 100) : '未命名画布',
      objects: {},
      order: [],
      revision: 1,
      updatedAt: now,
    }
    await fs.mkdir(this.dir, { recursive: true })
    await fs.writeFile(this.file(doc.id)!, JSON.stringify(doc))
    if (idempotencyKey) {
      const map = await this.readIdempotency()
      map[idempotencyKey] = { docId: doc.id, createdAt: now }
      await this.writeIdempotency(map)
    }
    return doc
  }

  async get(id: string): Promise<CanvasDoc | undefined> {
    const file = this.file(id)
    if (!file) {
      return undefined
    }
    try {
      const doc = JSON.parse(await fs.readFile(file, 'utf8')) as CanvasDoc
      return doc.id === id ? doc : undefined
    } catch {
      // 损坏的文档文件:视为不存在,让前端走新建
      return undefined
    }
  }

  /** 仅改名称(重命名不动对象、不冲突覆盖内容);文档不存在返回 undefined。 */
  async rename(id: string, name: unknown): Promise<CanvasDoc | undefined> {
    const doc = await this.get(id)
    if (!doc) {
      return undefined
    }
    doc.name =
      typeof name === 'string' && name.trim() ? name.trim().slice(0, 100) : '未命名画布'
    doc.revision += 1
    doc.updatedAt = new Date().toISOString()
    await this.writeDoc(id, doc)
    return doc
  }

  /**
   * 全量覆写保存(客户端防抖后的快照);返回盖了服务端时间戳与 revision 的文档。
   * payload.baseRevision 存在时做乐观并发校验:不一致抛 DocumentConflictError(PRD §9 A13)。
   */
  async save(id: string, payload: unknown): Promise<CanvasDoc | undefined> {
    const file = this.file(id)
    if (!file) {
      return undefined
    }
    const raw = payload as Partial<CanvasDoc> & { baseRevision?: unknown } | null
    if (
      !raw ||
      typeof raw !== 'object' ||
      raw.objects === null ||
      typeof raw.objects !== 'object'
    ) {
      throw new Error('文档格式错误:缺少 objects')
    }
    if (!Array.isArray(raw.order) || raw.order.some((k) => typeof k !== 'string')) {
      throw new Error('文档格式错误:order 必须是对象 id 数组')
    }
    const objectCount = Object.keys(raw.objects).length
    if (objectCount > MAX_OBJECTS) {
      throw new Error(`文档对象数超上限(${MAX_OBJECTS})`)
    }
    if (Array.isArray(raw.chat) && raw.chat.length > MAX_CHAT_MESSAGES) {
      throw new Error(`对话消息数超上限(${MAX_CHAT_MESSAGES})`)
    }
    if (
      raw.groups !== undefined &&
      (raw.groups === null || typeof raw.groups !== 'object' || Array.isArray(raw.groups) ||
        Object.keys(raw.groups).length > MAX_GROUPS)
    ) {
      throw new Error('文档格式错误:groups 必须是对象')
    }
    const current = await this.get(id)
    if (!current) {
      return undefined
    }
    const baseRevision = Number(raw.baseRevision)
    if (Number.isFinite(baseRevision) && baseRevision !== current.revision) {
      throw new DocumentConflictError(
        `文档已被其他会话更新(base ${baseRevision} ≠ server ${current.revision})`,
        current,
      )
    }
    const doc: CanvasDoc = {
      id,
      name:
        typeof raw.name === 'string' && raw.name.trim()
          ? raw.name.trim().slice(0, 100)
          : '未命名画布',
      objects: raw.objects,
      order: raw.order,
      revision: current.revision + 1,
      updatedAt: new Date().toISOString(),
    }
    if (raw.groups && typeof raw.groups === 'object' && !Array.isArray(raw.groups)) {
      doc.groups = raw.groups as Record<string, unknown>
    }
    if (Array.isArray(raw.chat)) {
      doc.chat = raw.chat
    }
    const serialized = JSON.stringify(doc)
    if (serialized.length > MAX_DOC_BYTES) {
      throw new Error('文档内容超过大小限制')
    }
    await this.writeDoc(id, doc, serialized)
    logger.debug('文档已保存', { docId: id, objects: objectCount, bytes: serialized.length })
    return doc
  }

  private async writeDoc(id: string, doc: CanvasDoc, serialized?: string): Promise<void> {
    const file = this.file(id)
    if (!file) {
      throw new Error(`非法文档 id:${id}`)
    }
    const body = serialized ?? JSON.stringify(doc)
    // 原子写:先临时文件后改名,避免崩溃留下半截 JSON
    const tmp = `${file}.tmp`
    await fs.mkdir(this.dir, { recursive: true })
    await fs.writeFile(tmp, body)
    await fs.rename(tmp, file)
  }

  async remove(id: string): Promise<boolean> {
    const file = this.file(id)
    if (!file) {
      return false
    }
    try {
      await fs.rm(file)
      return true
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code === 'ENOENT') {
        return false
      }
      logger.warn('删除文档失败', { docId: id, err })
      return false
    }
  }

  /** 列出全部文档(新的在前);目录不存在时返回空。 */
  async listAll(): Promise<DocumentSummary[]> {
    let entries: string[]
    try {
      entries = await fs.readdir(this.dir)
    } catch {
      return []
    }
    const summaries: DocumentSummary[] = []
    for (const entry of entries) {
      if (!entry.endsWith('.json') || entry.startsWith('_')) {
        continue
      }
      const doc = await this.get(entry.slice(0, -5))
      if (doc) {
        summaries.push({
          id: doc.id,
          name: doc.name,
          updatedAt: doc.updatedAt,
          objectCount: doc.order.length,
        })
      }
    }
    return summaries.sort((a, b) => (a.updatedAt < b.updatedAt ? 1 : -1))
  }
}
