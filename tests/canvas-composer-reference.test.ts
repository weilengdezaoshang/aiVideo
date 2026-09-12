import test from 'node:test'
import assert from 'node:assert/strict'
import {
  readReferenceWeight,
  videoDurationOptions,
  videoFormatOptions,
} from '../apps/web/canvas/flows/composer-reference.js'
import type { Capability } from '../apps/web/canvas/state/node-model.js'

const cloudVideo: Capability = {
  supported: true,
  models: [],
  ratios: ['16:9', '9:16', '1:1'],
  resolutions: [1280, 960],
  sizes: ['1280x720', '720x1280', '960x960'],
  referenceLimit: 0,
  durations: [5],
}

const localVideo: Capability = {
  supported: true,
  models: [],
  ratios: ['16:9', '9:16', '1:1'],
  resolutions: [512, 768],
  referenceLimit: 1,
  durations: [4, 6, 8, 12],
}

test('videoFormatOptions:云端精确档位直出并带比例标注', () => {
  const options = videoFormatOptions(cloudVideo)
  assert.deepEqual(
    options.map((option) => option.value),
    ['1280x720', '720x1280', '960x960'],
  )
  assert.equal(options[0].label, '1280×720(16:9)')
  assert.equal(options[1].label, '720×1280(9:16)')
  assert.equal(options[2].label, '960×960(1:1)')
})

test('videoFormatOptions:本地后端回退 ratios×resolutions 且 8 像素对齐', () => {
  const options = videoFormatOptions(localVideo)
  assert.equal(options.length, 6) // 3 比例 × 2 分辨率
  const byValue = new Map(options.map((option) => [option.value, option.label]))
  assert.equal(byValue.get('768x432'), '16:9 · 768') // 768*9/16=432
  assert.equal(byValue.get('432x768'), '9:16 · 768')
  assert.equal(byValue.get('768x768'), '1:1 · 768')
  for (const option of options) {
    const [w, h] = option.value.split('x').map(Number)
    assert.equal(w % 8, 0)
    assert.equal(h % 8, 0)
  }
})

test('videoDurationOptions:能力缺失时回退 4 秒', () => {
  assert.deepEqual(
    videoDurationOptions(cloudVideo).map((option) => option.value),
    ['5'],
  )
  const empty: Capability = { ...localVideo, durations: [] }
  assert.deepEqual(
    videoDurationOptions(empty).map((option) => option.value),
    ['4'],
  )
})

test('readReferenceWeight:契约外取值返回 undefined', () => {
  assert.equal(readReferenceWeight({ value: 'high' } as HTMLSelectElement), 'high')
  assert.equal(readReferenceWeight({ value: 'strong' } as HTMLSelectElement), undefined)
  assert.equal(readReferenceWeight(null), undefined)
})
