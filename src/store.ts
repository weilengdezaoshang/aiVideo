import { promises as fs } from 'node:fs'
import path from 'node:path'
import { randomBytes, randomUUID } from 'node:crypto'
import type { GenParams, ImageRecord } from './types.js'

export interface SaveImageInput {
  jobId: string
  provider: string
  params: GenParams
  ext: string
  data: Buffer | string
}

/**
 * 生成结果的磁盘持久化:图片写入 imagesDir,历史记录写入 JSON 文件,重启不丢。
 * 历史超过 limit 时裁剪最旧记录并删除对应文件。
 */
export class Store {
  private records: ImageRecord[] = []

  constructor(
    readonly imagesDir: string,
    private dbFile: string,
    private limit = 500,
  ) {}

  async init(): Promise<void> {
    await fs.mkdir(this.imagesDir, { recursive: true })
    try {
      const raw = JSON.parse(await fs.readFile(this.dbFile, 'utf8')) as { records?: ImageRecord[] }
      if (Array.isArray(raw.records)) {
        this.records = raw.records
      }
    } catch {
      // 首次启动还没有历史文件,属于正常情况
    }
  }

  list(limit = 100): ImageRecord[] {
    return this.records.slice(0, limit)
  }

  get(id: string): ImageRecord | undefined {
    return this.records.find((r) => r.id === id)
  }

  async save(input: SaveImageInput): Promise<ImageRecord> {
    const file = `img_${Date.now()}_${randomBytes(4).toString('hex')}.${input.ext}`
    await fs.writeFile(path.join(this.imagesDir, file), input.data)
    const record: ImageRecord = {
      id: randomUUID(),
      jobId: input.jobId,
      file,
      url: `/images/${file}`,
      provider: input.provider,
      params: input.params,
      createdAt: new Date().toISOString(),
    }
    this.records.unshift(record)
    await this.prune()
    return record
  }

  async remove(id: string): Promise<ImageRecord | undefined> {
    const idx = this.records.findIndex((r) => r.id === id)
    if (idx === -1) {
      return undefined
    }
    const [record] = this.records.splice(idx, 1)
    // 文件名由服务端生成,这里防御性地拒绝任何路径片段
    if (!record.file.includes('/') && !record.file.includes('..')) {
      await fs.rm(path.join(this.imagesDir, record.file), { force: true }).catch(() => {})
    }
    await this.persist()
    return record
  }

  private async prune(): Promise<void> {
    const removed = this.records.splice(this.limit)
    await Promise.all(
      removed.map((r) => fs.rm(path.join(this.imagesDir, r.file), { force: true }).catch(() => {})),
    )
    await this.persist()
  }

  private async persist(): Promise<void> {
    await fs.mkdir(path.dirname(this.dbFile), { recursive: true })
    await fs.writeFile(this.dbFile, JSON.stringify({ records: this.records }, null, 2))
  }
}
