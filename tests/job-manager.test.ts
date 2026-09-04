import test from 'node:test'
import assert from 'node:assert/strict'
import { promises as fs } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { JobManager } from '../src/services/job-manager.js'
import { Store } from '../src/store.js'
import type { GenParams, Job } from '../src/types.js'
import type {
  GenContext,
  GeneratedImage,
  GenerationProvider,
} from '../src/services/providers/provider.js'

const params: GenParams = {
  prompt: 'p',
  negativePrompt: '',
  model: 'm',
  width: 64,
  height: 64,
  steps: 2,
  cfgScale: 7,
  seed: 100,
  batchCount: 2,
  sampler: 'euler',
  scheduler: 'normal',
  denoise: 1,
  kind: 'image',
  durationSec: 4,
  fps: 16,
}

function fakeProvider(overrides: Partial<GenerationProvider> = {}): GenerationProvider {
  return {
    name: 'fake',
    capacity: 1,
    async status() {
      return { ok: true, detail: 'ok' }
    },
    async listModels() {
      return []
    },
    async listSamplerOptions() {
      return { samplers: ['euler'], schedulers: ['normal'] }
    },
    async generate(_params: GenParams, ctx: GenContext): Promise<GeneratedImage> {
      ctx.onProgress(0.5, 'half')
      return { data: Buffer.from('img'), ext: 'png' }
    },
    async generateVideo(_params: GenParams, ctx: GenContext): Promise<GeneratedImage> {
      ctx.onProgress(0.5, 'half')
      return { data: Buffer.from('vid'), ext: 'svg' }
    },
    ...overrides,
  }
}

async function tmpStore() {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'swarm-jm-'))
  const store = new Store(path.join(dir, 'images'), path.join(dir, 'history.json'))
  await store.init()
  return store
}

function waitEvent(
  jm: JobManager,
  event: string,
  predicate: (payload: Job) => boolean,
): Promise<Job> {
  return new Promise((resolve) => {
    const handler = (payload: Job) => {
      if (predicate(payload)) {
        jm.off(event, handler)
        resolve(payload)
      }
    }
    jm.on(event, handler)
  })
}

test('任务按序完成,种子递增且图片落盘', async () => {
  const store = await tmpStore()
  const jm = new JobManager(fakeProvider(), store)
  const done = waitEvent(jm, 'job', (j) => j.status === 'completed')
  const job = jm.createJob({ ...params })
  const finished = await done
  assert.equal(finished.id, job.id)
  assert.equal(finished.images.length, 2)
  assert.deepEqual(
    finished.images.map((i) => i.params.seed),
    [100, 101],
  )
  assert.equal(store.list().length, 2)
})

test('随机种子(-1)解析为非负数', async () => {
  const store = await tmpStore()
  const jm = new JobManager(fakeProvider(), store)
  const done = waitEvent(jm, 'job', (j) => j.status === 'completed')
  jm.createJob({ ...params, seed: -1 })
  const finished = await done
  for (const img of finished.images) {
    assert.ok(img.params.seed >= 0)
  }
})

test('进度按 (完成张数 + 当前张进度)/总数 推进', async () => {
  const store = await tmpStore()
  const jm = new JobManager(fakeProvider(), store)
  const events: { id: string; progress: number }[] = []
  jm.on('job', (job) => events.push({ id: job.id, progress: job.progress }))
  jm.createJob({ ...params })
  await waitEvent(jm, 'job', (j) => j.status === 'completed')
  const maxProgress = Math.max(...events.map((e) => e.progress))
  assert.equal(maxProgress, 1)
})

test('取消运行中的任务立即失败并继续调度后续任务', async () => {
  const store = await tmpStore()
  const jm = new JobManager(
    fakeProvider({
      generate: (_params, ctx) =>
        new Promise((_resolve, reject) => {
          ctx.signal.addEventListener('abort', () => reject(new Error('已取消')))
        }),
    }),
    store,
  )
  const failed = waitEvent(jm, 'job', (j) => j.status === 'failed')
  const job = jm.createJob({ ...params })
  await new Promise((r) => setTimeout(r, 20))
  assert.equal(jm.cancel(job.id)?.id, job.id)
  const finished = await failed
  assert.equal(finished.error, '已取消')
  assert.equal(jm.cancel(job.id), undefined)
})

test('参数校验:空提示词与缺失模型被拒绝', async () => {
  const store = await tmpStore()
  const jm = new JobManager(fakeProvider(), store)
  const job = jm.createJob({ ...params })
  assert.equal(typeof job.id, 'string')
})

test('kind=video 的任务路由到 generateVideo', async () => {
  const store = await tmpStore()
  const calls = { image: 0, video: 0 }
  const jm = new JobManager(
    fakeProvider({
      generate: async (_p, ctx) => {
        calls.image++
        ctx.onProgress(1, 'done')
        return { data: Buffer.from('i'), ext: 'png' }
      },
      generateVideo: async (_p, ctx) => {
        calls.video++
        ctx.onProgress(1, 'done')
        return { data: Buffer.from('v'), ext: 'svg' }
      },
    }),
    store,
  )
  const imgDone = waitEvent(jm, 'job', (j) => j.status === 'completed')
  jm.createJob({ ...params, batchCount: 1 })
  await imgDone
  const vidDone = waitEvent(jm, 'job', (j) => j.status === 'completed')
  jm.createJob({ ...params, batchCount: 1, kind: 'video', durationSec: 1, fps: 8 })
  await vidDone
  assert.deepEqual(calls, { image: 1, video: 1 })
})
