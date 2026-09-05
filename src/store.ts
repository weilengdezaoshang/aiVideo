import { promises as fs } from 'node:fs'
import path from 'node:path'
import { randomBytes, randomUUID } from 'node:crypto'
import { logger } from './logger.js'
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
      await fs.rm(path.join(this.imagesDir, record.file), { force: true }).catch((err) => {
        logger.warn('删除图片文件失败', { file: record.file, err })
      })
    }
    await this.persist()
    return record
  }

  /** 切换收藏标记并持久化,返回更新后的记录;不存在返回 undefined。 */
  async setStarred(id: string, starred: boolean): Promise<ImageRecord | undefined> {
    const record = this.records.find((r) => r.id === id)
    if (!record) {
      return undefined
    }
    record.starred = starred
    await this.persist()
    return record
  }

  private async prune(): Promise<void> {
    // 收藏记录无条件保留;未收藏记录最多保留 limit 条(从最旧一侧丢弃)
    const keep: ImageRecord[] = []
    const drop: ImageRecord[] = []
    let kept = 0
    for (const r of this.records) {
      if (r.starred) {
        keep.push(r)
      } else if (kept < this.limit) {
        keep.push(r)
        kept++
      } else {
        drop.push(r)
      }
    }
    this.records = keep
    await Promise.all(
      drop.map((r) =>
        fs.rm(path.join(this.imagesDir, r.file), { force: true }).catch((err) => {
          logger.warn('裁剪历史时删除图片文件失败', { file: r.file, err })
        }),
      ),
    )
    // 无论是否发生裁剪都要落盘:prune 是 save 的收尾步骤,承担历史持久化职责
    await this.persist()
  }

  private async persist(): Promise<void> {
    try {
      await fs.mkdir(path.dirname(this.dbFile), { recursive: true })
      await fs.writeFile(this.dbFile, JSON.stringify({ records: this.records }, null, 2))
    } catch (err) {
      // 历史写盘失败会导致重启后丢记录,必须留下现场再向上抛
      logger.error('历史记录写入失败', { dbFile: this.dbFile, err })
      throw err
    }
  }
}
