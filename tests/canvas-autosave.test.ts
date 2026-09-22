import './dom.js'
import test from 'node:test'
import assert from 'node:assert/strict'
import { createAutoSaver } from '../apps/web/canvas/state/autosave.js'

test('确认保存等待旧请求完成并保存最新分镜和 revision', async () => {
  const doc = { id: 'confirmed-save', name: '初始', revision: 1, objects: {}, order: [] }
  let release!: () => void
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  const snapshots: { name: string; baseRevision: number }[] = []
  const saver = createAutoSaver({
    docId: doc.id,
    getDoc: () => doc,
    fetchImpl: async (_url, init) => {
      snapshots.push(JSON.parse(String(init?.body)))
      if (snapshots.length === 1) {
        await gate
      }
      return new Response(JSON.stringify({ document: { revision: snapshots.length + 1 } }))
    },
  })
  const initial = saver.flush()
  doc.name = '最新分镜'
  let confirmed = false
  const confirm = saver.ensureSaved().then(() => {
    confirmed = true
  })
  await Promise.resolve()
  assert.equal(confirmed, false)
  assert.equal(snapshots.length, 1)
  release()
  await Promise.all([initial, confirm])
  assert.equal(confirmed, true)
  assert.deepEqual(
    snapshots.map(({ name, baseRevision }) => [name, baseRevision]),
    [
      ['初始', 1],
      ['最新分镜', 2],
    ],
  )
  assert.equal(doc.revision, 3)
})

test('确认保存遇到网络错误或冲突时拒绝，不强制覆盖服务端', async () => {
  const doc = { id: 'failed-save', name: '分镜', revision: 1, objects: {}, order: [] }
  let code = 503
  let calls = 0
  const saver = createAutoSaver({
    docId: doc.id,
    getDoc: () => doc,
    fetchImpl: async (_url, init) => {
      calls++
      assert.equal(JSON.parse(String(init?.body)).baseRevision, 1)
      return new Response(JSON.stringify({ error: code === 409 ? '版本冲突' : '服务不可用' }), {
        status: code,
      })
    },
  })
  await assert.rejects(saver.ensureSaved(), /服务不可用/)
  code = 409
  await assert.rejects(saver.ensureSaved(), /版本冲突/)
  assert.equal(saver.inConflict, true)
  await assert.rejects(saver.ensureSaved(), /版本冲突/)
  assert.equal(calls, 2)
  localStorage.removeItem(`gencanvas.draft.${doc.id}`)
})
