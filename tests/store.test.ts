import test from 'node:test'
import assert from 'node:assert/strict'
import { promises as fs } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { Store } from '../src/store.js'
import type { GenParams } from '../src/types.js'

const params: GenParams = {
  prompt: 'cat',
  negativePrompt: '',
  model: 'm',
  width: 512,
  height: 512,
  steps: 20,
  cfgScale: 7,
  seed: 1,
  batchCount: 1,
  sampler: 'euler',
  scheduler: 'normal',
  denoise: 1,
  kind: 'image',
  durationSec: 4,
  fps: 16,
}

async function tmpStore(limit = 500) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'swarm-mvp-'))
  const store = new Store(path.join(dir, 'images'), path.join(dir, 'history.json'), limit)
  await store.init()
  return { store, dir }
}

test('save 写入文件并生成记录,list 最新在前', async () => {
  const { store, dir } = await tmpStore()
  const a = await store.save({ jobId: 'j1', provider: 'mock', params, ext: 'svg', data: '<svg/>' })
  const b = await store.save({
    jobId: 'j1',
    provider: 'mock',
    params: { ...params, seed: 2 },
    ext: 'svg',
    data: '<svg/>',
  })
  assert.deepEqual(
    store.list().map((r) => r.id),
    [b.id, a.id],
  )
  await fs.access(path.join(dir, 'images', a.file))
  await fs.access(path.join(dir, 'images', b.file))
})

test('remove 同时删除记录与文件', async () => {
  const { store, dir } = await tmpStore()
  const rec = await store.save({ jobId: 'j', provider: 'mock', params, ext: 'svg', data: 'x' })
  const removed = await store.remove(rec.id)
  assert.equal(removed?.id, rec.id)
  assert.equal(store.list().length, 0)
  await assert.rejects(fs.access(path.join(dir, 'images', rec.file)))
  assert.equal(await store.remove('missing'), undefined)
})

test('历史超过上限时裁剪最旧记录并清理文件', async () => {
  const { store, dir } = await tmpStore(2)
  const recs = []
  for (let i = 0; i < 3; i++) {
    recs.push(await store.save({ jobId: 'j', provider: 'mock', params, ext: 'svg', data: 'x' }))
  }
  assert.deepEqual(
    store.list().map((r) => r.id),
    [recs[2].id, recs[1].id],
  )
  await assert.rejects(fs.access(path.join(dir, 'images', recs[0].file)))
})

test('重启后历史可从磁盘恢复', async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'swarm-mvp-'))
  const imagesDir = path.join(dir, 'images')
  const dbFile = path.join(dir, 'history.json')
  const first = new Store(imagesDir, dbFile)
  await first.init()
  const rec = await first.save({ jobId: 'j', provider: 'mock', params, ext: 'svg', data: '<svg/>' })

  const second = new Store(imagesDir, dbFile)
  await second.init()
  assert.equal(second.list().length, 1)
  assert.equal(second.get(rec.id)?.id, rec.id)
})
