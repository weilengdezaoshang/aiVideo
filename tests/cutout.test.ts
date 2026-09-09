import test from 'node:test'
import assert from 'node:assert/strict'
import sharp from 'sharp'
import { RembgCutoutProvider } from '../src/services/cutout/rembg-provider.js'
import { applyCutoutMask } from '../src/services/cutout/apply-mask.js'

test('rembg 适配器传递模型并返回 PNG Mask', async () => {
  const calls: string[] = []
  const fetchImpl = (async (input: URL | RequestInfo, init?: RequestInit) => {
    calls.push(String(input))
    assert.equal(init?.method, 'POST')
    assert.equal(init?.headers && (init.headers as Record<string, string>)['Content-Type'], 'image/png')
    return new Response(Buffer.from('mask'), {
      headers: { 'X-Cutout-Model': 'birefnet-general' },
    })
  }) as typeof fetch
  const provider = new RembgCutoutProvider({
    serviceUrl: 'http://127.0.0.1:7100/',
    model: 'birefnet-general',
    fetchImpl,
  })
  const result = await provider.createMask(Buffer.from('image'), 'image/png')
  assert.equal(result.model, 'birefnet-general')
  assert.deepEqual(result.data, Buffer.from('mask'))
  assert.equal(calls[0], 'http://127.0.0.1:7100/v1/masks?model=birefnet-general')
})

test('Mask 白色保留、黑色透明', async () => {
  const original = await sharp({
    create: { width: 2, height: 1, channels: 3, background: { r: 255, g: 0, b: 0 } },
  })
    .png()
    .toBuffer()
  const mask = await sharp(Buffer.from([255, 0]), { raw: { width: 2, height: 1, channels: 1 } })
    .png()
    .toBuffer()
  const output = await applyCutoutMask(original, mask)
  const { data, info } = await sharp(output).raw().toBuffer({ resolveWithObject: true })
  assert.equal(info.channels, 4)
  assert.equal(data[3], 255)
  assert.equal(data[7], 0)
})

test('拒绝与原图尺寸不同的 Mask', async () => {
  const original = await sharp({
    create: { width: 2, height: 2, channels: 3, background: 'red' },
  })
    .png()
    .toBuffer()
  const mask = await sharp(Buffer.from([255]), { raw: { width: 1, height: 1, channels: 1 } })
    .png()
    .toBuffer()
  await assert.rejects(applyCutoutMask(original, mask), /尺寸必须与原图一致/)
})
