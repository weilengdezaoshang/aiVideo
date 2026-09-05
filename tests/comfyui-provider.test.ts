import test from 'node:test'
import assert from 'node:assert/strict'
import {
  ComfyUIProvider,
  collectOutputFiles,
  pickVideoFile,
} from '../src/services/providers/comfyui-provider.js'
import type { GenContext } from '../src/services/providers/provider.js'
import type { GenParams, InitImage } from '../src/types.js'

const BASE = 'http://comfy.test:8188'

const videoParams: GenParams = {
  prompt: '一只奔跑的猫',
  negativePrompt: '模糊',
  model: 'ckpt.safetensors',
  width: 512,
  height: 512,
  steps: 12,
  cfgScale: 6,
  seed: -1,
  batchCount: 1,
  sampler: 'euler',
  scheduler: 'normal',
  denoise: 1,
  kind: 'video',
  durationSec: 2,
  fps: 16,
}

const initImage: InitImage = { data: Buffer.from('fake-jpeg'), ext: 'jpeg' }

function makeCtx(overrides: Partial<GenContext> = {}): { ctx: GenContext; progress: number[] } {
  const progress: number[] = []
  return {
    progress,
    ctx: {
      seed: 42,
      index: 0,
      onProgress: (p) => progress.push(p),
      signal: new AbortController().signal,
      ...overrides,
    },
  }
}

type Route = (url: string, init?: RequestInit) => Response

const jsonResponse = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

interface Harness {
  provider: ComfyUIProvider
  routes: Map<RegExp, Route>
  workflow: () => Record<string, { class_type: string; inputs: Record<string, unknown> }>
  viewUrl: () => string
}

/** 构造带 fake fetch 的 provider:按正则注册路由,未命中即失败,便于断言请求面。 */
function makeProvider(opts: Record<string, unknown> = {}): Harness {
  const captured: { workflow?: unknown; viewUrl?: string } = {}
  const routes = new Map<RegExp, Route>()
  const fetchImpl = (async (input: Parameters<typeof fetch>[0], init?: RequestInit) => {
    const url = String(input)
    for (const [pattern, handler] of routes) {
      if (pattern.test(url)) {
        return handler(url, init)
      }
    }
    throw new Error(`测试未路由的请求:${url}`)
  }) as typeof fetch
  routes.set(/\/upload\/image$/, () => jsonResponse({ name: 'swarm-ref-1.jpeg' }))
  routes.set(/\/prompt$/, (_url, init) => {
    captured.workflow = (JSON.parse(String(init?.body)) as { prompt: unknown }).prompt
    return jsonResponse({ prompt_id: 'p1' })
  })
  routes.set(/\/view\?/, (url) => {
    captured.viewUrl = url
    return new Response(new Uint8Array([0x00, 0x00, 0x00, 0x18, 0x66, 0x74, 0x79, 0x70]))
  })
  const provider = new ComfyUIProvider(BASE, { pollMs: 1, fetchImpl, ...opts })
  return {
    provider,
    routes,
    workflow: () =>
      captured.workflow as Record<string, { class_type: string; inputs: Record<string, unknown> }>,
    viewUrl: () => captured.viewUrl ?? '',
  }
}

test('generateVideo 全流程:上传首帧 → 提交 → 轮询 → 下载 mp4', async () => {
  const h = makeProvider({ videoModel: 'wan2.2_i2v_high_noise_14B_fp16.safetensors' })
  let historyCalls = 0
  h.routes.set(/\/history\//, () => {
    historyCalls++
    if (historyCalls < 3) {
      return jsonResponse({})
    }
    return jsonResponse({
      p1: {
        status: { status_str: 'success' },
        outputs: {
          v: {
            images: [
              { filename: 'SwarmUI_MVP_video_00001.mp4', subfolder: 'video', type: 'output' },
            ],
          },
        },
      },
    })
  })
  const { ctx, progress } = makeCtx()
  const result = await h.provider.generateVideo(videoParams, ctx, initImage)

  assert.equal(result.ext, 'mp4')
  assert.ok(result.data.length > 0)
  assert.equal(progress[progress.length - 1], 1)
  // 视频帧率 16fps × 2s = 32 帧,Wan 需为 4 的倍数
  assert.equal(h.workflow()['12'].inputs.length, 32)
  assert.equal(h.workflow()['2'].inputs.type, 'wan')
  assert.ok(h.viewUrl().includes('filename=SwarmUI_MVP_video_00001.mp4'))
  assert.ok(h.viewUrl().includes('subfolder=video'))
})

test('generateVideo 自动探测:checkpoint 列表中的 LTX 模型走 LTXV 工作流', async () => {
  const h = makeProvider()
  h.routes.set(/\/object_info\/CheckpointLoaderSimple$/, () =>
    jsonResponse({
      CheckpointLoaderSimple: {
        input: { required: { ckpt_name: [['foo.safetensors', 'ltx-video-2b-v0.9.safetensors']] } },
      },
    }),
  )
  h.routes.set(/\/object_info\/UNETLoader$/, () =>
    jsonResponse({ UNETLoader: { input: { required: { unet_name: [[]] } } } }),
  )
  h.routes.set(/\/history\//, () =>
    jsonResponse({
      p1: {
        status: { status_str: 'success' },
        outputs: { v: { videos: [{ filename: 'out.mp4', subfolder: '', type: 'output' }] } },
      },
    }),
  )
  const { ctx } = makeCtx()
  const result = await h.provider.generateVideo({ ...videoParams, durationSec: 4, fps: 16 }, ctx)

  assert.equal(result.ext, 'mp4')
  const wf = h.workflow()
  assert.equal(wf['12'].class_type, 'EmptyLTXVLatentVideo')
  assert.equal(wf['13'].inputs.frame_rate, 16)
  assert.equal(wf['12'].inputs.length, 65)
})

test('generateVideo 无可用视频模型时给出可操作的错误', async () => {
  const h = makeProvider()
  h.routes.set(/\/object_info\/CheckpointLoaderSimple$/, () =>
    jsonResponse({
      CheckpointLoaderSimple: { input: { required: { ckpt_name: [['foo.safetensors']] } } },
    }),
  )
  h.routes.set(/\/object_info\/UNETLoader$/, () =>
    jsonResponse({ UNETLoader: { input: { required: { unet_name: [[]] } } } }),
  )
  const { ctx } = makeCtx()
  await assert.rejects(h.provider.generateVideo(videoParams, ctx), /未找到可用的视频模型/)
})

test('generateVideo 超过整体超时后报错', async () => {
  const h = makeProvider({ videoModel: 'wan.safetensors', videoTimeoutMs: 20 })
  h.routes.set(/\/history\//, () => jsonResponse({}))
  const { ctx } = makeCtx()
  await assert.rejects(h.provider.generateVideo(videoParams, ctx), /超时/)
})

test('generateVideo 识别取消信号并中断 ComfyUI', async () => {
  const h = makeProvider({ videoModel: 'wan.safetensors' })
  let interrupted = false
  h.routes.set(/\/interrupt$/, () => {
    interrupted = true
    return jsonResponse({})
  })
  const ac = new AbortController()
  ac.abort()
  const { ctx } = makeCtx({ signal: ac.signal })
  await assert.rejects(h.provider.generateVideo(videoParams, ctx), /已取消/)
  assert.equal(interrupted, true)
})

test('collectOutputFiles 同时收集 images 与 videos 键,pickVideoFile 优先视频', () => {
  const files = collectOutputFiles({
    outputs: {
      a: { images: [{ filename: 'preview.png', subfolder: '', type: 'temp' }] },
      b: { videos: [{ filename: 'final.webm', subfolder: 'v', type: 'output' }] },
    },
  })
  assert.equal(files.length, 2)
  const picked = pickVideoFile(files)
  assert.equal(picked?.filename, 'final.webm')
  assert.equal(
    pickVideoFile([{ filename: 'only.png', subfolder: '', type: 'output' }])?.filename,
    'only.png',
  )
})
