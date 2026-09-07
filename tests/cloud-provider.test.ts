import test from 'node:test'
import assert from 'node:assert/strict'
import { CloudImagesProvider } from '../src/services/providers/cloud-provider.js'
import type { GenParams } from '../src/types.js'

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
    /仅支持纯文生图/,
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
