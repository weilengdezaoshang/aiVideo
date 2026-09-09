import { EventEmitter } from 'node:events'
import { randomInt, randomUUID } from 'node:crypto'
import { promises as fs } from 'node:fs'
import path from 'node:path'
import { logger } from '../logger.js'
import type { GenParams, InitImage, Job } from '../types.js'
import type { GenContext, GeneratedImage, GenerationProvider } from './providers/provider.js'
import type { Store } from '../store.js'

/** 任务快照落盘防抖间隔:进度事件高频,合并写盘 */
const SNAPSHOT_DEBOUNCE_MS = 1000

/**
 * 生成任务调度:FIFO 队列,并发上限取自 provider.capacity。
 * 事件(EventEmitter):
 *   - 'job'   任务状态/进度变化(携带完整 Job)
 *   - 'image' 一张图片落盘完成(携带 { jobId, image })
 * 参考图(InitImage)仅在任务执行期间保存在内存,不写入 Job 本体,避免大 Buffer 进入 JSON。
 * 传入 snapshotFile 时任务状态防抖落盘,重启后恢复:进行中任务标记失败"服务已重启",
 * 已完成任务保留供客户端对账(PRD §8.2/§8.4-5)。
 */
export class JobManager extends EventEmitter {
  private queue: string[] = []
  private jobs = new Map<string, Job>()
  private running = new Set<string>()
  private aborts = new Map<string, AbortController>()
  private initImages = new Map<string, InitImage>()
  private masks = new Map<string, InitImage>()
  private snapshotTimer: NodeJS.Timeout | null = null

  constructor(
    private provider: GenerationProvider,
    private store: Store,
    private snapshotFile?: string,
  ) {
    super()
  }

  /** 启动时恢复任务快照:queued/running → failed(服务已重启);损坏文件视为无历史。 */
  async restore(): Promise<void> {
    if (!this.snapshotFile) {
      return
    }
    let raw: { jobs?: unknown }
    try {
      raw = JSON.parse(await fs.readFile(this.snapshotFile, 'utf8')) as { jobs?: unknown }
    } catch {
      return
    }
    if (!Array.isArray(raw.jobs)) {
      return
    }
    const interrupted: string[] = []
    for (const entry of raw.jobs) {
      const job = entry as Job
      if (!job || typeof job.id !== 'string' || !job.params) {
        continue
      }
      if (job.status === 'queued' || job.status === 'running') {
        job.status = 'failed'
        job.error = '服务已重启,任务中断'
        job.message = job.error
        job.finishedAt = new Date().toISOString()
        job.progress = 0
        interrupted.push(job.id)
      }
      this.jobs.set(job.id, job)
    }
    if (interrupted.length > 0) {
      logger.warn('服务重启中断了进行中的任务', { count: interrupted.length, ids: interrupted })
    }
    logger.debug('任务快照已恢复', { total: this.jobs.size })
  }

  private markSnapshotDirty(): void {
    if (!this.snapshotFile || this.snapshotTimer) {
      return
    }
    this.snapshotTimer = setTimeout(() => {
      this.snapshotTimer = null
      void this.flush()
    }, SNAPSHOT_DEBOUNCE_MS)
  }

  /** 立即写盘(防抖冲刷 / 优雅关闭时调用)。 */
  async flush(): Promise<void> {
    if (!this.snapshotFile) {
      return
    }
    if (this.snapshotTimer) {
      clearTimeout(this.snapshotTimer)
      this.snapshotTimer = null
    }
    const payload = JSON.stringify({ jobs: [...this.jobs.values()] })
    const tmp = `${this.snapshotFile}.tmp`
    try {
      await fs.mkdir(path.dirname(this.snapshotFile), { recursive: true })
      await fs.writeFile(tmp, payload)
      await fs.rename(tmp, this.snapshotFile)
    } catch (err) {
      // 快照失败不影响生成主流程,但必须留现场
      logger.error('任务快照写入失败', { file: this.snapshotFile, err })
    }
  }

  createJob(
    params: GenParams,
    initImage?: InitImage,
    maskImage?: InitImage,
    clientRef?: string,
    group?: { groupId: string; slot: number; directionTitle: string },
  ): Job {
    const job: Job = {
      id: randomUUID(),
      status: 'queued',
      params,
      batchCount: params.batchCount,
      hasInitImage: Boolean(initImage),
      clientRef,
      progress: 0,
      message: '排队中',
      images: [],
      createdAt: new Date().toISOString(),
    }
    if (group) {
      job.groupId = group.groupId
      job.slot = group.slot
      job.directionTitle = group.directionTitle
    }
    this.jobs.set(job.id, job)
    if (initImage) {
      this.initImages.set(job.id, initImage)
    }
    if (maskImage) {
      this.masks.set(job.id, maskImage)
    }
    this.queue.push(job.id)
    this.markSnapshotDirty()
    this.emit('job', job)
    logger.debug('任务入队', {
      jobId: job.id,
      kind: params.kind,
      batch: params.batchCount,
      model: params.model,
      img2img: Boolean(initImage),
      inpaint: Boolean(maskImage),
    })
    this.pump()
    return job
  }

  getJob(id: string): Job | undefined {
    return this.jobs.get(id)
  }

  /** 运行中热切换生成后端(BYOK 设置):进行中任务按原后端跑完,此后调度的任务用新后端。 */
  setProvider(provider: GenerationProvider): void {
    this.provider = provider
    this.pump()
  }

  /** 进行中的任务(排队 + 运行),新的在前。 */
  listActive(): Job[] {
    return [...this.jobs.values()]
      .filter((j) => j.status === 'queued' || j.status === 'running')
      .sort((a, b) => (a.createdAt < b.createdAt ? 1 : -1))
  }

  /** 最近的任务(含已完成/失败),新的在前,用于任务历史面板。 */
  listRecent(limit = 30): Job[] {
    return [...this.jobs.values()]
      .sort((a, b) => (a.createdAt < b.createdAt ? 1 : -1))
      .slice(0, limit)
  }

  /** 取消排队或运行中的任务;已结束的任务返回 undefined。 */
  cancel(id: string): Job | undefined {
    const job = this.jobs.get(id)
    if (!job || (job.status !== 'queued' && job.status !== 'running')) {
      return undefined
    }
    if (job.status === 'queued') {
      this.queue = this.queue.filter((qid) => qid !== id)
      job.status = 'failed'
      job.error = '已取消'
      job.message = '已取消'
      job.finishedAt = new Date().toISOString()
      this.initImages.delete(id)
      this.masks.delete(id)
      this.markSnapshotDirty()
      this.emit('job', job)
      logger.info('排队任务已取消', { jobId: id })
    } else {
      this.aborts.get(id)?.abort()
    }
    return job
  }

  private pump(): void {
    while (this.running.size < this.provider.capacity && this.queue.length > 0) {
      const id = this.queue.shift()!
      const job = this.jobs.get(id)
      if (!job) {
        continue
      }
      this.running.add(id)
      void this.runJob(job)
    }
  }

  private async runJob(job: Job): Promise<void> {
    const startedMs = Date.now()
    job.status = 'running'
    job.startedAt = new Date().toISOString()
    job.message = '准备中'
    this.markSnapshotDirty()
    this.emit('job', job)
    logger.debug('任务开始执行', { jobId: job.id })
    const ac = new AbortController()
    this.aborts.set(job.id, ac)
    const initImage = this.initImages.get(job.id)
    const maskImage = this.masks.get(job.id)
    const baseSeed = job.params.seed >= 0 ? job.params.seed : randomInt(0, 2 ** 31 - 1)
    try {
      const run = (
        index: number,
        onProgress: GenContext['onProgress'],
      ): Promise<GeneratedImage> => {
        const ctx: GenContext = {
          seed: baseSeed + index,
          index,
          signal: ac.signal,
          onProgress,
        }
        return job.params.kind === 'video'
          ? this.provider.generateVideo(job.params, ctx, initImage)
          : this.provider.generate(job.params, ctx, initImage, maskImage)
      }
      for (let index = 0; index < job.batchCount; index++) {
        let lastEmit = 0
        const generated = await run(index, (p, message) => {
          const clamped = Math.min(1, Math.max(0, p))
          job.progress = Math.min(0.999, (index + clamped) / job.batchCount)
          job.message = message
          const now = Date.now()
          if (now - lastEmit > 100 || clamped >= 1) {
            lastEmit = now
            this.emit('job', job)
          }
        })
        const record = await this.store.save({
          jobId: job.id,
          provider: this.provider.name,
          params: { ...job.params, seed: baseSeed + index },
          data: generated.data,
          ext: generated.ext,
        })
        job.images.push(record)
        this.markSnapshotDirty()
        this.emit('image', { jobId: job.id, image: record })
      }
      job.status = 'completed'
      job.progress = 1
      job.message = '完成'
      job.finishedAt = new Date().toISOString()
      this.markSnapshotDirty()
      logger.info('任务完成', {
        jobId: job.id,
        images: job.images.length,
        elapsedMs: Date.now() - startedMs,
      })
      this.emit('job', job)
    } catch (err) {
      job.status = 'failed'
      job.error = (err as Error).message || '生成失败'
      job.message = job.error
      job.finishedAt = new Date().toISOString()
      this.markSnapshotDirty()
      if (job.error === '已取消') {
        logger.info('运行中任务已取消', { jobId: job.id, images: job.images.length })
      } else {
        logger.error('任务失败', {
          jobId: job.id,
          err,
          elapsedMs: Date.now() - startedMs,
        })
      }
      this.emit('job', job)
    } finally {
      this.aborts.delete(job.id)
      this.initImages.delete(job.id)
      this.masks.delete(job.id)
      this.running.delete(job.id)
      this.pruneFinishedJobs()
      this.pump()
    }
  }

  /** 已结束的任务只保留最近 200 个,防止内存无限增长(图片本身已持久化)。 */
  private pruneFinishedJobs(): void {
    const finished = [...this.jobs.values()]
      .filter((j) => j.status === 'completed' || j.status === 'failed')
      .sort((a, b) => ((a.finishedAt ?? '') < (b.finishedAt ?? '') ? -1 : 1))
    while (finished.length > 200) {
      const oldest = finished.shift()!
      this.jobs.delete(oldest.id)
    }
  }
}
