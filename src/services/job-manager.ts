import { EventEmitter } from 'node:events'
import { randomInt, randomUUID } from 'node:crypto'
import type { GenParams, InitImage, Job } from '../types.js'
import type { GenContext, GeneratedImage, GenerationProvider } from './providers/provider.js'
import type { Store } from '../store.js'

/**
 * 生成任务调度:FIFO 队列,并发上限取自 provider.capacity。
 * 事件(EventEmitter):
 *   - 'job'   任务状态/进度变化(携带完整 Job)
 *   - 'image' 一张图片落盘完成(携带 { jobId, image })
 * 参考图(InitImage)仅在任务执行期间保存在内存,不写入 Job 本体,避免大 Buffer 进入 JSON。
 */
export class JobManager extends EventEmitter {
  private queue: string[] = []
  private jobs = new Map<string, Job>()
  private running = new Set<string>()
  private aborts = new Map<string, AbortController>()
  private initImages = new Map<string, InitImage>()

  constructor(
    private provider: GenerationProvider,
    private store: Store,
  ) {
    super()
  }

  createJob(params: GenParams, initImage?: InitImage): Job {
    const job: Job = {
      id: randomUUID(),
      status: 'queued',
      params,
      batchCount: params.batchCount,
      hasInitImage: Boolean(initImage),
      progress: 0,
      message: '排队中',
      images: [],
      createdAt: new Date().toISOString(),
    }
    this.jobs.set(job.id, job)
    if (initImage) {
      this.initImages.set(job.id, initImage)
    }
    this.queue.push(job.id)
    this.emit('job', job)
    this.pump()
    return job
  }

  getJob(id: string): Job | undefined {
    return this.jobs.get(id)
  }

  /** 进行中的任务(排队 + 运行),新的在前。 */
  listActive(): Job[] {
    return [...this.jobs.values()]
      .filter((j) => j.status === 'queued' || j.status === 'running')
      .sort((a, b) => (a.createdAt < b.createdAt ? 1 : -1))
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
      this.emit('job', job)
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
    job.status = 'running'
    job.startedAt = new Date().toISOString()
    job.message = '准备中'
    this.emit('job', job)
    const ac = new AbortController()
    this.aborts.set(job.id, ac)
    const initImage = this.initImages.get(job.id)
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
          : this.provider.generate(job.params, ctx, initImage)
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
        this.emit('image', { jobId: job.id, image: record })
      }
      job.status = 'completed'
      job.progress = 1
      job.message = '完成'
      job.finishedAt = new Date().toISOString()
      this.emit('job', job)
    } catch (err) {
      job.status = 'failed'
      job.error = (err as Error).message || '生成失败'
      job.message = job.error
      job.finishedAt = new Date().toISOString()
      this.emit('job', job)
    } finally {
      this.aborts.delete(job.id)
      this.initImages.delete(job.id)
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
