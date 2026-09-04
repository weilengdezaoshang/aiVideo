import test from 'node:test'
import assert from 'node:assert/strict'
import { MockProvider } from '../src/services/providers/mock-provider.js'
import type { GenParams } from '../src/types.js'

const params: GenParams = {
  prompt: 'a cute <cat> & 宇航员猫',
  negativePrompt: 'blurry',
  model: 'mock-diffusion-xl',
  width: 512,
  height: 256,
  steps: 3,
  cfgScale: 7,
  seed: 42,
  batchCount: 1,
  sampler: 'euler',
  scheduler: 'normal',
  denoise: 1,
}

test('Mock 后端按步上报进度并产出转义后的 SVG', async () => {
  const provider = new MockProvider(1)
  const progress: number[] = []
  const out = await provider.generate(params, {
    seed: 42,
    index: 0,
    signal: new AbortController().signal,
    onProgress: (p) => progress.push(p),
  })
  assert.equal(out.ext, 'svg')
  const svg = out.data.toString('utf8')
  assert.ok(svg.includes('seed 42'))
  assert.ok(svg.includes('&lt;cat&gt;'))
  assert.ok(svg.includes('宇航员猫'))
  assert.equal(progress.at(-1), 1)
  assert.ok((await provider.listModels()).length >= 3)
  assert.equal((await provider.status()).ok, true)
})

test('相同种子产出相同内容(可复现)', async () => {
  const provider = new MockProvider(1)
  const ctx = { seed: 7, index: 0, signal: new AbortController().signal, onProgress: () => {} }
  const a = await provider.generate(params, ctx)
  const b = await provider.generate(params, ctx)
  assert.equal(a.data.equals(b.data), true)
})

test('abort 后生成立即失败', async () => {
  const provider = new MockProvider(50)
  const ac = new AbortController()
  const pending = provider.generate(params, {
    seed: 1,
    index: 0,
    signal: ac.signal,
    onProgress: () => ac.abort(),
  })
  await assert.rejects(pending, /已取消/)
})

test('图生图:参考图按 (1 - denoise) 垫底嵌入输出', async () => {
  const provider = new MockProvider(1)
  const initImage = { data: Buffer.from('fake-png-bytes'), ext: 'png' }
  const out = await provider.generate(
    { ...params, denoise: 0.4 },
    { seed: 9, index: 0, signal: new AbortController().signal, onProgress: () => {} },
    initImage,
  )
  const svg = out.data.toString('utf8')
  assert.ok(svg.includes('data:image/png;base64,'))
  assert.ok(svg.includes('opacity="0.60"'))
  assert.ok(svg.includes('img2img 0.40'))
})

test('采样器选项可用', async () => {
  const provider = new MockProvider(1)
  const { samplers, schedulers } = await provider.listSamplerOptions()
  assert.ok(samplers.includes('euler'))
  assert.ok(schedulers.includes('karras'))
})
