import test from 'node:test'
import assert from 'node:assert/strict'
import {
  CloudImagesProvider,
  resolveCloudImageModel,
} from '../src/services/providers/cloud-provider.js'
import type { GenParams } from '../src/types.js'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { JobManager } from '../src/services/job-manager.js'
import { Store } from '../src/store.js'
import type { Job } from '../src/types.js'

const params = (): GenParams => ({
  prompt: '一只戴墨镜的猫',
  negativePrompt: '模糊',
  model: 'cogview-4-250304',
  width: 1024,
  height: 768,
  steps: 20,
  cfgScale: 7,
  seed: -1,
  batchCount: 1,
  sampler: 'euler',
  scheduler: 'normal',
  denoise: 1,
  kind: 'image',
  durationSec: 4,
  fps: 16,
})

const ctx = {
  seed: 1,
  index: 0,
  onProgress: () => {},
  signal: new AbortController().signal,
}

interface Call {
  url: string
  init: RequestInit
}

/** 可编程的 fetch 桩:依次消费 respond 列表,记录请求 */
function stubFetch(
  respond: Array<{ status: number; body: unknown; headers?: Record<string, string> }>,
) {
  const calls: Call[] = []
  const impl = (async (url: string | URL, init: RequestInit = {}) => {
    calls.push({ url: String(url), init })
    const next = respond.shift()
    if (!next) {
      throw new Error('stub: 没有更多响应')
    }
    const headers = new Headers(next.headers)
    const raw =
      typeof next.body === 'string' || Buffer.isBuffer(next.body)
        ? next.body
        : JSON.stringify(next.body)
    return new Response(raw as string, { status: next.status, headers })
  }) as unknown as typeof fetch
  return { impl, calls }
}

const pngBytes = Buffer.from('89504e47', 'hex')

const bailianUrl = 'https://ws-test.cn-beijing.maas.aliyuncs.com/api/v1'
const bailianResult = {
  output: {
    choices: [
      { message: { content: [{ text: 'done' }, { image: 'https://cdn.example/qwen.png' }] } },
    ],
  },
}

function bailian(fetchImpl: typeof fetch, baseUrl = bailianUrl, apiKey = 'sk-test') {
  return new CloudImagesProvider({
    vendor: 'aliyun',
    baseUrl,
    model: 'qwen-image-2.0',
    apiKey,
    imageTimeoutMs: 5000,
    fetchImpl,
  })
}

test('百炼:原生协议、尺寸、种子、单张请求与无鉴权图片下载', async () => {
  const { impl, calls } = stubFetch([
    { status: 200, body: bailianResult },
    { status: 200, body: pngBytes },
  ])
  const image = await bailian(impl).generate(params(), ctx)
  assert.deepEqual(image.data, pngBytes)
  assert.equal(calls[0].url, `${bailianUrl}/services/aigc/multimodal-generation/generation`)
  const req = JSON.parse(String(calls[0].init.body))
  assert.equal(req.model, 'qwen-image-2.0')
  assert.deepEqual(req.input.messages, [{ role: 'user', content: [{ text: params().prompt }] }])
  assert.deepEqual(req.parameters, {
    size: '1024*768',
    n: 1,
    prompt_extend: false,
    watermark: false,
    negative_prompt: '模糊',
    seed: 1,
  })
  assert.equal(new Headers(calls[0].init.headers).get('Authorization'), 'Bearer sk-test')
  assert.equal(new Headers(calls[1].init.headers).get('Authorization'), null)
})

test('百炼:完整 endpoint 与域名两种配置均不会重复拼接路径', async () => {
  const endpoint = `${bailianUrl}/services/aigc/multimodal-generation/generation`
  for (const base of [`${endpoint}/`, 'https://ws-test.cn-beijing.maas.aliyuncs.com/']) {
    const { impl, calls } = stubFetch([
      { status: 200, body: bailianResult },
      { status: 200, body: pngBytes },
    ])
    await bailian(impl, base).generate(params(), ctx)
    assert.equal(calls[0].url, endpoint)
  }
})

test('百炼:参考图使用多模态编辑协议', async () => {
  const { impl, calls } = stubFetch([
    { status: 200, body: bailianResult },
    { status: 200, body: pngBytes },
  ])
  const image = { data: pngBytes, ext: 'png' as const }
  await bailian(impl).generate(params(), ctx, image)

  const req = JSON.parse(String(calls[0].init.body))
  assert.equal(req.model, 'qwen-image-edit-plus')
  assert.match(req.input.messages[0].content[0].image, /^data:image\/png;base64,/)
  assert.deepEqual(req.input.messages[0].content[1], { text: params().prompt })
  assert.equal(req.parameters.size, '1024*768')
  assert.equal(req.parameters.seed, 1)
})

test('百炼:配置检查零请求;无 Key、不支持蒙版和非法尺寸不请求上游', async () => {
  const { impl, calls } = stubFetch([])
  const provider = bailian(impl)
  assert.match((await provider.status()).detail, /未验证鉴权/)
  await assert.rejects(bailian(impl, bailianUrl, '').generate(params(), ctx), /未配置/)
  await assert.rejects(provider.generate({ ...params(), width: 64, height: 64 }, ctx), /尺寸/)
  const image = { data: pngBytes, ext: 'png' as const }
  await assert.rejects(provider.generate(params(), ctx, image, image), /暂不支持蒙版/)
  assert.equal(calls.length, 0)
})

test('百炼:鉴权错误和 HTTP 200 业务错误均失败且脱敏,不自动重试', async () => {
  for (const status of [401, 200]) {
    const { impl, calls } = stubFetch([
      { status, body: { code: 'InvalidApiKey', message: 'invalid sk-test' } },
    ])
    await assert.rejects(bailian(impl).generate(params(), ctx), (err: Error) => {
      assert.match(err.message, /InvalidApiKey/)
      assert.ok(!err.message.includes('sk-test'))
      return true
    })
    assert.equal(calls.length, 1)
  }
})

test('百炼:缺失图片与下载失败均不能报告成功', async () => {
  const missing = stubFetch([{ status: 200, body: { output: { choices: [] } } }])
  await assert.rejects(bailian(missing.impl).generate(params(), ctx), /没有生成图片/)
  const download = stubFetch([
    { status: 200, body: bailianResult },
    { status: 403, body: 'expired' },
  ])
  await assert.rejects(bailian(download.impl).generate(params(), ctx), /图像下载失败/)
})

test('百炼桩响应集成:任务完成、PNG 落盘、历史重新加载', async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'aivideo-bailian-'))
  try {
    const png = Buffer.from(
      'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=',
      'base64',
    )
    const { impl } = stubFetch([
      { status: 200, body: bailianResult },
      { status: 200, body: png },
    ])
    const store = new Store(path.join(dir, 'images'), path.join(dir, 'history.json'))
    await store.init()
    const manager = new JobManager(bailian(impl), store)
    const done = new Promise<Job>((resolve) =>
      manager.on('job', (job: Job) => {
        if (job.status === 'completed' || job.status === 'failed') {
          resolve(job)
        }
      }),
    )
    manager.createJob({ ...params(), model: 'qwen-image-2.0' })
    const job = await done
    assert.equal(job.status, 'completed', job.error)
    assert.equal(job.images.length, 1)
    assert.deepEqual(await readFile(path.join(store.imagesDir, job.images[0].file)), png)
    const restored = new Store(store.imagesDir, path.join(dir, 'history.json'))
    await restored.init()
    assert.equal(restored.list()[0].params.model, 'qwen-image-2.0')
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
})

test('智谱:请求体形状与 data[].url 下载链路', async () => {
  const { impl, calls } = stubFetch([
    { status: 200, body: { created: 1, data: [{ url: 'https://cdn.example/img.png' }] } },
    { status: 200, body: pngBytes, headers: { 'content-type': 'image/png' } },
  ])
  const provider = new CloudImagesProvider({
    vendor: 'zhipu',
    baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
    model: 'cogview-4-250304',
    apiKey: 'sk-test',
    imageTimeoutMs: 5000,
    fetchImpl: impl,
  })
  const image = await provider.generate(params(), ctx)
  assert.equal(image.ext, 'png')
  assert.deepEqual([...image.data.subarray(0, 4)], [...pngBytes])

  const req = JSON.parse(String(calls[0].init.body))
  assert.equal(calls[0].url, 'https://open.bigmodel.cn/api/paas/v4/images/generations')
  assert.equal(req.model, 'cogview-4-250304')
  assert.equal(req.prompt, '一只戴墨镜的猫')
  assert.equal(req.size, '1024x768', '尺寸映射为宽x高')
  assert.equal(req.negativePrompt, undefined, '反向提示词不发给云端(模型不支持)')
  assert.equal(
    calls[0].init.headers && (calls[0].init.headers as Record<string, string>).Authorization,
    'Bearer sk-test',
  )
})

test('硅基流动:image_size 字段与 images[].url 解析', async () => {
  const { impl, calls } = stubFetch([
    { status: 200, body: { images: [{ url: 'https://cdn.example/a.webp' }], seed: 1 } },
    { status: 200, body: pngBytes, headers: { 'content-type': 'image/webp' } },
  ])
  const provider = new CloudImagesProvider({
    vendor: 'siliconflow',
    baseUrl: 'https://api.siliconflow.cn/v1',
    model: 'black-forest-labs/FLUX.1-schnell',
    apiKey: 'sk-test',
    imageTimeoutMs: 5000,
    fetchImpl: impl,
  })
  const image = await provider.generate(params(), ctx)
  assert.equal(image.ext, 'webp')
  const req = JSON.parse(String(calls[0].init.body))
  assert.equal(req.image_size, '1024x768')
  assert.equal(req.batch_size, 1)
})

test('OpenAI 兼容:b64_json 解码为 Buffer', async () => {
  const { impl, calls } = stubFetch([
    { status: 200, body: { data: [{ b64_json: pngBytes.toString('base64') }] } },
  ])
  const provider = new CloudImagesProvider({
    vendor: 'openai',
    baseUrl: 'https://api.openai.com/v1',
    model: 'gpt-image-1',
    apiKey: 'sk-test',
    imageTimeoutMs: 5000,
    fetchImpl: impl,
  })
  const image = await provider.generate(params(), ctx)
  assert.equal(image.ext, 'png')
  assert.deepEqual([...image.data.subarray(0, 4)], [...pngBytes])
  assert.equal(calls.length, 1, 'b64 无需下载步骤')
})

test('厂商错误体:透出 message 供画布错误卡展示', async () => {
  const { impl } = stubFetch([
    { status: 400, body: { error: { code: '1211', message: '模型不存在' } } },
  ])
  const provider = new CloudImagesProvider({
    vendor: 'zhipu',
    baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
    model: 'no-such-model',
    apiKey: 'sk-test',
    imageTimeoutMs: 5000,
    fetchImpl: impl,
  })
  await assert.rejects(() => provider.generate(params(), ctx), /模型不存在/)
})

test('i2i/蒙版请求:显式报错(当前云 API 不通用)', async () => {
  const { impl, calls } = stubFetch([])
  const provider = new CloudImagesProvider({
    vendor: 'zhipu',
    baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
    model: 'm',
    apiKey: 'sk-test',
    imageTimeoutMs: 5000,
    fetchImpl: impl,
  })
  await assert.rejects(
    () => provider.generate(params(), ctx, { data: Buffer.from('x'), ext: 'png' }),
    /暂不支持参考图/,
  )
  await assert.rejects(() => provider.generateVideo(), /视频生成暂未接入/)
  assert.equal(calls.length, 0, '不支持的能力不应发出请求')
})

test('status 不发网络请求:有 Key 正常,无 Key 提示配置', async () => {
  const { impl, calls } = stubFetch([])
  const withKey = new CloudImagesProvider({
    vendor: 'zhipu',
    baseUrl: 'https://x',
    model: 'm',
    apiKey: 'sk-test',
    imageTimeoutMs: 1000,
    fetchImpl: impl,
  })
  const ok = await withKey.status()
  assert.equal(ok.ok, true)
  const noKey = new CloudImagesProvider({
    vendor: 'zhipu',
    baseUrl: 'https://x',
    model: 'm',
    apiKey: '',
    imageTimeoutMs: 1000,
    fetchImpl: impl,
  })
  const bad = await noKey.status()
  assert.equal(bad.ok, false)
  assert.match(bad.detail, /未配置图像 API Key/)
  assert.equal(calls.length, 0, '健康检查绝不产生计费请求')
})

test('硅基流动 i2i:Kontext 模型 + image 参数(dataURL)', async () => {
  const { impl, calls } = stubFetch([
    { status: 200, body: { images: [{ url: 'https://cdn.example/i2i.png' }] } },
    { status: 200, body: pngBytes, headers: { 'content-type': 'image/png' } },
  ])
  const provider = new CloudImagesProvider({
    vendor: 'siliconflow',
    baseUrl: 'https://api.siliconflow.cn/v1',
    model: 'black-forest-labs/FLUX.1-schnell',
    apiKey: 'sk-test',
    imageTimeoutMs: 5000,
    fetchImpl: impl,
  })
  const initImage = { data: Buffer.from('src-bytes'), ext: 'png' as const }
  const image = await provider.generate(params(), ctx, initImage)
  assert.equal(image.ext, 'png')
  const req = JSON.parse(String(calls[0].init.body))
  assert.equal(req.model, 'black-forest-labs/FLUX.1-Kontext-dev', 'i2i 自动切 Kontext 模型')
  assert.match(String(req.image), /^data:image\/png;base64,/)
  assert.equal(req.image_size, '1024x768')
})

test('硅基流动蒙版重绘:Fill 模型 + image_mask(白 = 重绘)', async () => {
  const { impl, calls } = stubFetch([
    { status: 200, body: { images: [{ url: 'https://cdn.example/fill.png' }] } },
    { status: 200, body: pngBytes, headers: { 'content-type': 'image/png' } },
  ])
  const provider = new CloudImagesProvider({
    vendor: 'siliconflow',
    baseUrl: 'https://api.siliconflow.cn/v1',
    model: 'black-forest-labs/FLUX.1-schnell',
    apiKey: 'sk-test',
    imageTimeoutMs: 5000,
    fetchImpl: impl,
  })
  const initImage = { data: Buffer.from('src'), ext: 'png' as const }
  const maskImage = { data: Buffer.from('mask'), ext: 'png' as const }
  const image = await provider.generate(params(), ctx, initImage, maskImage)
  assert.equal(image.ext, 'png')
  const req = JSON.parse(String(calls[0].init.body))
  assert.equal(req.model, 'black-forest-labs/FLUX.1-Fill-dev', '蒙版重绘自动切 Fill 模型')
  assert.match(String(req.image), /^data:image\/png;base64,/)
  assert.match(String(req.image_mask), /^data:image\/png;base64,/)
})

test('智谱蒙版重绘:不支持则显式报错', async () => {
  const { impl, calls } = stubFetch([])
  const provider = new CloudImagesProvider({
    vendor: 'zhipu',
    baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
    model: 'cogview-4-250304',
    apiKey: 'sk-test',
    imageTimeoutMs: 5000,
    fetchImpl: impl,
  })
  await assert.rejects(
    () =>
      provider.generate(
        params(),
        ctx,
        { data: Buffer.from('s'), ext: 'png' },
        { data: Buffer.from('m'), ext: 'png' },
      ),
    /暂不支持蒙版局部重绘/,
  )
  assert.equal(calls.length, 0)
})

test('翻译:中文检测与 chat/completions 请求形状', async () => {
  const { hasChinese, translateToEnglish } =
    await import('../src/services/providers/cloud-provider.js')
  assert.equal(hasChinese('一只戴墨镜的猫'), true)
  assert.equal(hasChinese('a cat with sunglasses'), false)

  const { impl, calls } = stubFetch([
    {
      status: 200,
      body: { choices: [{ message: { content: 'A cat wearing sunglasses, bokeh lights' } }] },
    },
  ])
  const translated = await translateToEnglish({
    apiKey: 'sk-test',
    baseUrl: 'https://api.siliconflow.cn/v1',
    model: 'Qwen/Qwen2.5-7B-Instruct',
    prompt: '一只戴墨镜的猫',
    signal: new AbortController().signal,
    fetchImpl: impl,
  })
  assert.equal(translated, 'A cat wearing sunglasses, bokeh lights')
  const req = JSON.parse(String(calls[0].init.body))
  assert.equal(req.model, 'Qwen/Qwen2.5-7B-Instruct')
  assert.equal(req.temperature, 0.1, '确定性翻译用低温')
  assert.match(req.messages[0].content, /Translate the user text to English/)
})

test('百炼模型路由：参考图选 Edit，文生图及显式编辑模型保持不变', () => {
  assert.equal(resolveCloudImageModel('aliyun', 'qwen-image-2.0', true), 'qwen-image-edit-plus')
  assert.equal(resolveCloudImageModel('aliyun', 'qwen-image-2.0', false), 'qwen-image-2.0')
  assert.equal(resolveCloudImageModel('aliyun', 'qwen-image-edit-max', true), 'qwen-image-edit-max')
  assert.equal(resolveCloudImageModel('openai', 'qwen-image-2.0', true), 'qwen-image-2.0')
})
