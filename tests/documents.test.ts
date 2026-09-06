import test from 'node:test'
import assert from 'node:assert/strict'
import { promises as fs } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { DocumentStore } from '../src/documents.js'

async function tmpStore(): Promise<{ store: DocumentStore; dir: string }> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'gencanvas-docs-'))
  const store = new DocumentStore(dir)
  await store.init()
  return { store, dir }
}

test('create 生成合法空文档并可 get 读回', async () => {
  const { store } = await tmpStore()
  const doc = await store.create()
  assert.match(doc.id, /^[a-f0-9-]{36}$/)
  assert.equal(doc.name, '未命名画布')
  assert.deepEqual(doc.objects, {})
  assert.deepEqual(doc.order, [])
  const loaded = await store.get(doc.id)
  assert.equal(loaded?.id, doc.id)
})

test('save 全量覆写并盖服务端时间戳;重启后可读回', async () => {
  const { store, dir } = await tmpStore()
  const doc = await store.create('我的画布')
  const saved = await store.save(doc.id, {
    name: '改名了',
    objects: { obj1: { kind: 'image', x: 1 }, obj2: { kind: 'placeholder', x: 2 } },
    order: ['obj1', 'obj2'],
  })
  assert.equal(saved?.name, '改名了')
  assert.equal(saved?.order.length, 2)
  assert.ok(saved?.updatedAt >= doc.updatedAt)

  const reborn = new DocumentStore(dir)
  await reborn.init()
  const loaded = await reborn.get(doc.id)
  assert.equal(loaded?.name, '改名了')
  assert.deepEqual(loaded?.order, ['obj1', 'obj2'])
})

test('save 拒绝非法形状与超限对象数', async () => {
  const { store } = await tmpStore()
  const doc = await store.create()
  await assert.rejects(() => store.save(doc.id, { name: 'x' }), /缺少 objects/)
  await assert.rejects(() => store.save(doc.id, { objects: {}, order: [1] }), /order/)
  const objects: Record<string, unknown> = {}
  for (let i = 0; i < 5001; i++) {
    objects[`o${i}`] = { kind: 'image' }
  }
  await assert.rejects(() => store.save(doc.id, { objects, order: [] }), /超上限/)
})

test('非法 id 被拒绝(防目录穿越)', async () => {
  const { store } = await tmpStore()
  assert.equal(await store.get('../etc/passwd'), undefined)
  assert.equal(await store.save('../../x', { objects: {}, order: [] }), undefined)
  assert.equal(await store.remove('no-slash-allowed'), false)
})

test('listAll 按更新时间新的在前;remove 后消失', async () => {
  const { store } = await tmpStore()
  const a = await store.create('A')
  const b = await store.create('B')
  await new Promise((r) => setTimeout(r, 10))
  await store.save(b.id, { objects: { x: { kind: 'image' } }, order: ['x'] })

  const list = await store.listAll()
  assert.equal(list.length, 2)
  assert.equal(list[0].id, b.id)
  assert.equal(list[0].objectCount, 1)
  assert.equal(list[1].id, a.id)

  assert.equal(await store.remove(b.id), true)
  assert.equal(await store.get(b.id), undefined)
  assert.equal(await store.remove(b.id), false)
})
