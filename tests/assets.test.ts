import test from 'node:test'
import assert from 'node:assert/strict'
import { promises as fs } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import sharp from 'sharp'
import { AssetStore } from '../src/assets.js'

async function tmpStore(): Promise<{ store: AssetStore; dir: string }> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'gencanvas-assets-'))
  const store = new AssetStore(dir)
  await store.init()
  return { store, dir }
}

test('save 图片资产:原图 + 两级缩略图 + 元数据齐全', async () => {
  const { store, dir } = await tmpStore()
  const png = await sharp({
    create: { width: 800, height: 600, channels: 3, background: '#369' },
  })
    .png()
    .toBuffer()
  const record = await store.save(png, 'png', 'image')

  assert.equal(record.width, 800)
  assert.equal(record.height, 600)
  assert.equal(record.hasThumbs, true)
  const assetDir = path.join(dir, record.id)
  for (const name of ['original.png', 't256.webp', 't1024.webp', 'meta.json']) {
    await assert.doesNotReject(() => fs.access(path.join(assetDir, name)))
  }
  const thumb = await sharp(path.join(assetDir, 't256.webp')).metadata()
  // inside + withoutEnlargement:800×600 按宽度限制缩为 256×192
  assert.equal(thumb.width, 256)
  assert.equal(thumb.height, 192)

  const urls = store.urls(record.id)
  assert.equal(urls?.original, `/assets/${record.id}/original.png`)
  assert.equal(urls?.thumb256, `/assets/${record.id}/t256.webp`)
})

test('save 视频资产:不生成缩略图但正常入库', async () => {
  const { store } = await tmpStore()
  const record = await store.save(Buffer.from('fake-video-bytes'), 'mp4', 'video')
  assert.equal(record.kind, 'video')
  assert.equal(record.hasThumbs, false)
  const urls = store.urls(record.id)
  assert.equal(urls?.thumb256, null)
})

test('save 坏图字节:降级为只存原图,不抛错', async () => {
  const { store } = await tmpStore()
  const record = await store.save(Buffer.from('not-an-image-at-all'), 'png', 'image')
  assert.equal(record.hasThumbs, false)
  assert.equal(record.width, 0)
})

test('save 拒绝不支持的扩展名;jpeg 统一为 jpg', async () => {
  const { store } = await tmpStore()
  await assert.rejects(() => store.save(Buffer.from('x'), 'exe', 'image'), /不支持的图片格式/)
  const record = await store.save(
    await sharp({ create: { width: 4, height: 4, channels: 3, background: '#000' } })
      .jpeg()
      .toBuffer(),
    'jpeg',
    'image',
  )
  assert.equal(record.ext, 'jpg')
})

test('重启后 init 恢复资产索引', async () => {
  const { store, dir } = await tmpStore()
  const png = await sharp({ create: { width: 10, height: 10, channels: 3, background: '#f00' } })
    .png()
    .toBuffer()
  const record = await store.save(png, 'png', 'image')

  const reborn = new AssetStore(dir)
  await reborn.init()
  assert.deepEqual(reborn.get(record.id), record)
  assert.equal(reborn.urls(record.id)?.original, `/assets/${record.id}/original.png`)
})

test('get/urls 对未知或非法 id 返回 undefined', async () => {
  const { store } = await tmpStore()
  assert.equal(store.get('no-such-id'), undefined)
  assert.equal(store.urls('no-such-id'), undefined)
  assert.equal(store.filePath('../etc/passwd', 'original'), undefined)
})

test('remove 删除资产目录与索引', async () => {
  const { store, dir } = await tmpStore()
  const png = await sharp({ create: { width: 8, height: 8, channels: 3, background: '#0f0' } })
    .png()
    .toBuffer()
  const record = await store.save(png, 'png', 'image')
  const removed = await store.remove(record.id)
  assert.equal(removed?.id, record.id)
  await assert.rejects(fs.access(path.join(dir, record.id)))
  assert.equal(store.get(record.id), undefined)
  assert.equal(await store.remove(record.id), undefined)
})
