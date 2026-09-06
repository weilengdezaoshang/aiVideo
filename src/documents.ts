import { promises as fs } from 'node:fs'
import path from 'node:path'
import { randomUUID } from 'node:crypto'
import { logger } from './logger.js'

/** 画布文档(PRD §7.1):对象与谱系;服务端只守卫形状与大小,内容语义归前端。 */
export interface CanvasDoc {
  id: string
  name: string
  /** id → 对象(图像/视频/占位/错误卡);只存 assetId 引用不存 blob */
  objects: Record<string, unknown>
  /** 渲染顺序(对象 id) */
  order: string[]
  updatedAt: string
}

export interface DocumentSummary {
  id: string
  name: string
  updatedAt: string
  objectCount: number
}

const DOC_ID_RE = /^[a-f0-9-]{36}$/
/** 文档序列化上限:5000 对象的引用 + 谱系参数绰绰有余,防异常 payload 撑爆磁盘 */
const MAX_DOC_BYTES = 5 * 1024 * 1024
const MAX_OBJECTS = 5000

export class DocumentStore {
  constructor(private dir: string) {}

  async init(): Promise<void> {
    await fs.mkdir(this.dir, { recursive: true })
  }

  private file(id: string): string | undefined {
    return DOC_ID_RE.test(id) ? path.join(this.dir, `${id}.json`) : undefined
  }

  async create(name?: unknown): Promise<CanvasDoc> {
    const now = new Date().toISOString()
    const doc: CanvasDoc = {
      id: randomUUID(),
      name: typeof name === 'string' && name.trim() ? name.trim().slice(0, 100) : '未命名画布',
      objects: {},
      order: [],
      updatedAt: now,
    }
    await fs.mkdir(this.dir, { recursive: true })
    await fs.writeFile(this.file(doc.id)!, JSON.stringify(doc))
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

  /** 全量覆写保存(客户端防抖后的快照);返回盖了服务端时间戳的文档。 */
  async save(id: string, payload: unknown): Promise<CanvasDoc | undefined> {
    const file = this.file(id)
    if (!file) {
      return undefined
    }
    const raw = payload as Partial<CanvasDoc> | null
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
    const doc: CanvasDoc = {
      id,
      name:
        typeof raw.name === 'string' && raw.name.trim()
          ? raw.name.trim().slice(0, 100)
          : '未命名画布',
      objects: raw.objects,
      order: raw.order,
      updatedAt: new Date().toISOString(),
    }
    const serialized = JSON.stringify(doc)
    if (serialized.length > MAX_DOC_BYTES) {
      throw new Error('文档内容超过大小限制')
    }
    // 原子写:先临时文件后改名,避免崩溃留下半截 JSON
    const tmp = `${file}.tmp`
    await fs.mkdir(this.dir, { recursive: true })
    await fs.writeFile(tmp, serialized)
    await fs.rename(tmp, file)
    logger.debug('文档已保存', { docId: id, objects: objectCount, bytes: serialized.length })
    return doc
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
      if (!entry.endsWith('.json')) {
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
