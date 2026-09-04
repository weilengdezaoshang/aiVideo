import test from 'node:test'
import assert from 'node:assert/strict'
import { parseGenParams, parseInitImage } from '../src/validate.js'

const validBase = { prompt: 'cat', model: 'm' }

test('parseGenParams 填充默认值并夹紧越界参数', () => {
  const result = parseGenParams({
    ...validBase,
    width: 99999,
    height: 10,
    steps: 500,
    cfgScale: 99,
    seed: -5,
    batchCount: 0,
  })
  assert.equal(result.ok, true)
  if (!result.ok) {
    return
  }
  const p = result.params
  assert.equal(p.width, 2048)
  assert.equal(p.height, 64)
  assert.equal(p.steps, 150)
  assert.equal(p.cfgScale, 30)
  assert.equal(p.seed, -1)
  assert.equal(p.batchCount, 1)
  assert.equal(p.sampler, 'euler')
  assert.equal(p.scheduler, 'normal')
  assert.equal(p.denoise, 1)
})

test('parseGenParams 保留合法的采样器与重绘幅度', () => {
  const result = parseGenParams({
    ...validBase,
    sampler: 'dpmpp_2m',
    scheduler: 'karras',
    denoise: 0.55,
  })
  assert.equal(result.ok, true)
  if (!result.ok) {
    return
  }
  assert.equal(result.params.sampler, 'dpmpp_2m')
  assert.equal(result.params.scheduler, 'karras')
  assert.equal(result.params.denoise, 0.55)
})

test('parseGenParams 拒绝空提示词与缺失模型', () => {
  assert.deepEqual(parseGenParams({ prompt: ' ', model: 'm' }), {
    ok: false,
    error: '提示词(prompt)不能为空',
  })
  assert.deepEqual(parseGenParams({ prompt: 'a' }), { ok: false, error: '缺少模型(model)' })
  assert.equal(parseGenParams(null).ok, false)
})

test('parseInitImage:未携带时返回 undefined', () => {
  for (const absent of [undefined, null, '']) {
    const r = parseInitImage(absent)
    assert.equal(r.ok, true)
    if (r.ok) {
      assert.equal(r.image, undefined)
    }
  }
})

test('parseInitImage:接受合法 data URL', () => {
  const r = parseInitImage(`data:image/png;base64,${Buffer.from('abc').toString('base64')}`)
  assert.equal(r.ok, true)
  if (r.ok && r.image) {
    assert.equal(r.image.ext, 'png')
    assert.equal(r.image.data.toString(), 'abc')
  }
  const jpg = parseInitImage(`data:image/jpg;base64,${Buffer.from('x').toString('base64')}`)
  assert.equal(jpg.ok, true)
  if (jpg.ok && jpg.image) {
    assert.equal(jpg.image.ext, 'jpeg')
  }
})

test('parseInitImage:拒绝非法类型与过大内容', () => {
  assert.equal(parseInitImage('data:image/gif;base64,AAAA').ok, false)
  assert.equal(parseInitImage('not-a-data-url').ok, false)
  assert.equal(parseInitImage(42).ok, false)
  const big = Buffer.alloc(8 * 1024 * 1024 + 1).toString('base64')
  assert.equal(parseInitImage(`data:image/png;base64,${big}`).ok, false)
})
