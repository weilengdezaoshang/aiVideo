import test from 'node:test'
import assert from 'node:assert/strict'

import {
  DRAFT_STEPS,
  FULL_STEPS,
  draftSize,
  fullQualityParams,
  isDraftParams,
  lockedSeed,
  seedOf,
} from '../apps/web/canvas/flows/draft-mode.js'

test('草稿尺寸:边长减半并对齐 8,最小 64、最大 2048', () => {
  assert.deepEqual(draftSize({ width: 1024, height: 1024 }), { width: 512, height: 512 })
  assert.deepEqual(draftSize({ width: 1360, height: 768 }), { width: 680, height: 384 })
  assert.deepEqual(draftSize({ width: 68, height: 68 }), { width: 64, height: 64 })
  assert.deepEqual(draftSize({ width: 4096, height: 4096 }), { width: 2048, height: 2048 })
})

test('草稿判定:步数不高于草稿档即为草稿,缺失或异常步数不算', () => {
  assert.equal(isDraftParams({ steps: DRAFT_STEPS }), true)
  assert.equal(isDraftParams({ steps: 6 }), true)
  assert.equal(isDraftParams({ steps: FULL_STEPS }), false)
  assert.equal(isDraftParams({ steps: 0 }), false)
  assert.equal(isDraftParams({ steps: -8 }), false)
  assert.equal(isDraftParams({}), false)
  assert.equal(isDraftParams({ steps: '8' }), false)
})

test('高配重出:恢复默认步数,边长翻倍回原档,其余参数原样保留', () => {
  const full = fullQualityParams({
    prompt: '一只猫',
    steps: DRAFT_STEPS,
    width: 512,
    height: 512,
    seed: 123,
    model: 'mock-test',
  })
  assert.equal(full.steps, FULL_STEPS)
  assert.equal(full.width, 1024)
  assert.equal(full.height, 1024)
  assert.equal(full.seed, 123)
  assert.equal(full.prompt, '一只猫')
  assert.equal(full.model, 'mock-test')
  // 尺寸已在上限的草稿保持上限,不再放大
  assert.equal(fullQualityParams({ steps: 8, width: 1024, height: 1024 }).width, 2048)
  assert.equal(fullQualityParams({ steps: 8, width: 2048, height: 2048 }).width, 2048)
  // 缺尺寸时只恢复步数,不臆造尺寸
  assert.deepEqual(fullQualityParams({ steps: 8, prompt: 'x' }), { steps: FULL_STEPS, prompt: 'x' })
})

test('种子锁定:仅接受非负整数种子,未启用、留空或非法输入回落随机', () => {
  assert.equal(lockedSeed(false, '123'), -1)
  assert.equal(lockedSeed(true, '123'), 123)
  assert.equal(lockedSeed(true, '0'), 0)
  assert.equal(lockedSeed(true, ''), -1)
  assert.equal(lockedSeed(true, '   '), -1)
  assert.equal(lockedSeed(true, 'abc'), -1)
  assert.equal(lockedSeed(true, '-3'), -1)
  assert.equal(lockedSeed(true, '2.5'), -1)
  assert.equal(lockedSeed(true, String(2 ** 31)), -1)
})

test('谱系种子:只认非负整数,历史任务(未固化,seed=-1)没有可锁种子', () => {
  assert.equal(seedOf({ seed: 7 }), 7)
  assert.equal(seedOf({ seed: 0 }), 0)
  assert.equal(seedOf({ seed: -1 }), null)
  assert.equal(seedOf({}), null)
  assert.equal(seedOf(undefined), null)
  assert.equal(seedOf('not-an-object'), null)
  assert.equal(seedOf({ seed: '7' }), null)
})
