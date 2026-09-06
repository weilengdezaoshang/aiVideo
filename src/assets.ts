import { promises as fs } from 'node:fs'
import path from 'node:path'
import { randomUUID } from 'node:crypto'
import sharp from 'sharp'
import { logger } from './logger.js'

export type AssetKind = 'image' | 'video'

export interface AssetRecord {
  id: string
  kind: AssetKind
  /** 原始文件扩展名(不含点,小写) */
  ext: string
  width: number
  height: number
  /** 缩略图是否生成成功;失败时降级为只存原图,不阻塞生成主流程 */
  hasThumbs: boolean
}

export interface AssetUrls {
  original: string
  thumb256: string | null
  thumb1024: string | null
}

const IMAGE_EXTS = new Set(['png', 'jpeg', 'jpg', 'webp'])
const VIDEO_EXTS = new Set(['mp4', 'webm'])

const THUMB_SIZES = [256, 1024] as const

/**
 * 画布资源存储:每个资产一个目录 data/assets/<id>/,
 * 含 original.<ext> + meta.json + 256/1024 两级 WebP 缩略图。
 * 画布文档只存 assetId 引用;缩略图供远缩放视图使用,避免大纹理拖垮渲染。
 */
export class AssetStore {
  private meta = new Map<string, AssetRecord>()

  constructor(private rootDir: string) {}

  async init(): Promise<void> {
    await fs.mkdir(this.rootDir, { recursive: true })
    // 重启后恢复资产索引:逐目录读 meta.json,损坏的目录跳过并告警
    for (const entry of await fs.readdir(this.rootDir, { withFileTypes: true })) {
      if (!entry.isDirectory()) {
        continue
      }
      try {
        const raw = JSON.parse(
          await fs.readFile(path.join(this.rootDir, entry.name, 'meta.json'), 'utf8'),
        ) as AssetRecord
        if (raw.id === entry.name) {
          this.meta.set(raw.id, raw)
        }
      } catch (err) {
        logger.warn('资产元数据读取失败,已跳过', { dir: entry.name, err })
      }
    }
  }

  get(id: string): AssetRecord | undefined {
    return this.meta.get(id)
  }

  /** HTTP 路径(供前端直接引用);id 非法或不存在时返回 undefined。 */
  urls(id: string): AssetUrls | undefined {
    const record = this.meta.get(id)
    if (!record) {
      return undefined
    }
    return {
      original: `/assets/${record.id}/original.${record.ext}`,
      thumb256: record.hasThumbs ? `/assets/${record.id}/t256.webp` : null,
      thumb1024: record.hasThumbs ? `/assets/${record.id}/t1024.webp` : null,
    }
  }

  /** 原始文件绝对路径;目录穿越类 id 一律拒绝。 */
  filePath(id: string, variant: 'original' | 'meta' | 't256' | 't1024'): string | undefined {
    const record = this.meta.get(id)
    if (!record || !/^[a-f0-9-]{36}$/.test(id)) {
      return undefined
    }
    const name =
      variant === 'original'
        ? `original.${record.ext}`
        : variant === 'meta'
          ? 'meta.json'
          : `${variant}.webp`
    return path.join(this.rootDir, id, name)
  }

  /**
   * 保存资产:写原图 → 生成缩略图(视频跳过;失败降级)→ 写 meta。
   * 任一磁盘错误向上抛,由调用方决定是否影响主流程。
   */
  async save(data: Buffer, ext: string, kind: AssetKind): Promise<AssetRecord> {
    const normalized = ext.toLowerCase().replace(/^\./, '')
    const valid = kind === 'video' ? VIDEO_EXTS : IMAGE_EXTS
    if (!valid.has(normalized)) {
      throw new Error(`不支持的${kind === 'video' ? '视频' : '图片'}格式:${normalized}`)
    }
    // jpeg 统一存为 jpg 扩展,减少 URL 形态
    const storedExt = normalized === 'jpeg' ? 'jpg' : normalized
    const id = randomUUID()
    const dir = path.join(this.rootDir, id)
    await fs.mkdir(dir, { recursive: true })
    await fs.writeFile(path.join(dir, `original.${storedExt}`), data)

    let width = 0
    let height = 0
    let hasThumbs = false
    if (kind === 'image') {
      try {
        const image = sharp(data, { failOn: 'none' }).rotate()
        const meta = await image.metadata()
        width = meta.width ?? 0
        height = meta.height ?? 0
        for (const size of THUMB_SIZES) {
          await sharp(data, { failOn: 'none' })
            .rotate()
            .resize(size, size, { fit: 'inside', withoutEnlargement: true })
            .webp({ quality: 82 })
            .toFile(path.join(dir, `t${size}.webp`))
        }
        hasThumbs = true
      } catch (err) {
        // 解码/缩放失败不阻塞资产保存:画布降级用原图
        logger.warn('缩略图生成失败,降级为原图', { assetId: id, err })
      }
    }

    const record: AssetRecord = { id, kind, ext: storedExt, width, height, hasThumbs }
    await fs.writeFile(path.join(dir, 'meta.json'), JSON.stringify(record, null, 2))
    this.meta.set(id, record)
    logger.debug('资产已保存', { assetId: id, kind, bytes: data.length, hasThumbs })
    return record
  }

  async remove(id: string): Promise<AssetRecord | undefined> {
    const record = this.meta.get(id)
    if (!record || !/^[a-f0-9-]{36}$/.test(id)) {
      return undefined
    }
    await fs.rm(path.join(this.rootDir, id), { recursive: true, force: true })
    this.meta.delete(id)
    return record
  }
}
