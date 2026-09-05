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

test('收藏标记可持久化且收藏记录不被裁剪', async () => {
  const { store, dir } = await tmpStore(2)
  // 灌满 2 条未收藏记录,把最旧的 a 标星,再继续灌 3 条:
  // 若收藏不豁免裁剪,a 最早入列必然先被清除
  const a = await store.save({ jobId: 'j', provider: 'mock', params, ext: 'svg', data: 'a' })
  const b = await store.save({ jobId: 'j', provider: 'mock', params, ext: 'svg', data: 'b' })
  await store.setStarred(a.id, true)
  const c = await store.save({ jobId: 'j', provider: 'mock', params, ext: 'svg', data: 'c' })
  const d = await store.save({ jobId: 'j', provider: 'mock', params, ext: 'svg', data: 'd' })
  const e = await store.save({ jobId: 'j', provider: 'mock', params, ext: 'svg', data: 'e' })

  assert.equal(store.get(a.id)?.starred, true)
  // 未收藏的 b、c 超出 limit=2 被裁剪;收藏的 a 与最近的 d、e 幸存
  assert.equal(store.get(b.id), undefined)
  assert.equal(store.get(c.id), undefined)
  for (const rec of [a, d, e]) {
    await fs.access(path.join(dir, 'images', rec.file))
  }
  // 重启后收藏标记仍在
  const second = new Store(path.join(dir, 'images'), path.join(dir, 'history.json'))
  await second.init()
  assert.equal(second.get(a.id)?.starred, true)
  assert.equal(second.list().length, 3)
})

test('list 支持关键字 / 模型 / 类型 / 收藏过滤', async () => {
  const { store } = await tmpStore()
  const mk = async (prompt: string, model: string, kind: 'image' | 'video', starred = false) => {
    const rec = await store.save({
      jobId: 'j',
      provider: 'mock',
      params: { ...params, prompt, model, kind },
      ext: 'svg',
      data: 'x',
    })
    if (starred) {
      await store.setStarred(rec.id, true)
    }
    return rec
  }
  const cat = await mk('一只宇航员猫', 'm1', 'image')
  const dog = await mk('一只柴犬 in 雪地', 'm1', 'image')
  const _sea = await mk('沉船与深海', 'm2', 'image')
  const vid = await mk('星空延时 video', 'm2', 'video', true)

  assert.deepEqual(
    store.list(100, { q: '猫' }).map((r) => r.id),
    [cat.id],
  )
  assert.deepEqual(
    store.list(100, { q: '雪地' }).map((r) => r.id),
    [dog.id],
  )
  assert.deepEqual(store.list(100, { q: 'DEEP'.toLowerCase() }).length, 0)
  assert.equal(store.list(100, { model: 'm2' }).length, 2)
  assert.equal(store.list(100, { kind: 'video' }).length, 1)
  assert.equal(store.list(100, { starred: true }).map((r) => r.id)[0], vid.id)
  assert.equal(store.list(100, { starred: false }).length, 3)
  // 组合过滤:模型 m2 + 类型 video
  assert.equal(store.list(100, { model: 'm2', kind: 'video' }).length, 1)
  assert.equal(store.list(100, { model: 'm1', kind: 'video' }).length, 0)
  assert.equal(store.list(100).length, 4)
})
