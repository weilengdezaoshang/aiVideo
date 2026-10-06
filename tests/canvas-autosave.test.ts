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
  doc.name = '有未保存编辑的分镜'
  await assert.rejects(saver.ensureSaved(), /服务不可用/)
  code = 409
  await assert.rejects(saver.ensureSaved(), /版本冲突/)
  assert.equal(saver.inConflict, true)
  await assert.rejects(saver.ensureSaved(), /版本冲突/)
  assert.equal(calls, 2)
  localStorage.removeItem(`gencanvas.draft.${doc.id}`)
})

test('只读助手确认 clean 画布不发保存，返回已确认 revision', async () => {
  const doc = { id: 'agent-readonly', name: '画布', revision: 8, objects: {}, order: [] }
  let calls = 0
  const saver = createAutoSaver({
    docId: doc.id,
    getDoc: () => doc,
    fetchImpl: async () => {
      calls++
      return Response.json({ document: { revision: 9 } })
    },
  })
  assert.equal(await saver.ensureSaved(), 8)
  assert.equal(await saver.ensureSaved(), 8)
  assert.equal(calls, 0)
  doc.name = '真实编辑'
  assert.equal(await saver.ensureSaved(), 9)
  assert.equal(calls, 1)
  assert.equal(await saver.ensureSaved(), 9)
  assert.equal(calls, 1)
})

test('unload 与旧保存串行，旧确认不清除新草稿，下一次使用新 revision', async () => {
  const { loadLocalDraft, clearLocalDraft } =
    await import('../apps/web/canvas/state/document-api.js')
  const doc = { id: 'save-race', name: '旧稿', revision: 1, objects: {}, order: [] }
  const calls: {
    body: { name: string; baseRevision: number }
    keepalive?: boolean
    resolve: (response: Response) => void
  }[] = []
  const saver = createAutoSaver({
    docId: doc.id,
    getDoc: () => doc,
    debounceMs: 60000,
    fetchImpl: async (_url, init) =>
      new Promise<Response>((resolve) => {
        calls.push({
          body: JSON.parse(String(init?.body)) as { name: string; baseRevision: number },
          keepalive: init?.keepalive,
          resolve,
        })
      }),
  })
  saver.markDirty()
  const flushed = saver.flush()
  doc.name = '最新稿'
  saver.markDirty()
  saver.flushOnUnload()
  assert.equal(calls.length, 1, '不能并发发送相同 revision')
  assert.equal(loadLocalDraft(doc.id)?.name, '最新稿', '编辑即落草稿，无须等待防抖')
  calls[0].resolve(Response.json({ document: { revision: 2 } }))
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(calls.length, 2)
  assert.deepEqual(calls[1].body.name, '最新稿')
  assert.equal(calls[1].body.baseRevision, 2)
  assert.equal(calls[1].keepalive, true)
  assert.equal(loadLocalDraft(doc.id)?.name, '最新稿')
  calls[1].resolve(Response.json({ error: '断网' }, { status: 503 }))
  await flushed
  assert.equal(loadLocalDraft(doc.id)?.name, '最新稿', '未确认的新稿始终可恢复')
  clearLocalDraft(doc.id)
})

test('未编辑的页面退出不覆盖既存草稿；冲突后不自动重发', async () => {
  const { loadLocalDraft, writeLocalDraft, clearLocalDraft } =
    await import('../apps/web/canvas/state/document-api.js')
  const doc = { id: 'clean-close', name: '服务器版本', revision: 1, objects: {}, order: [] }
  writeLocalDraft(doc.id, { ...doc, name: '未恢复草稿' })
  let calls = 0
  const saver = createAutoSaver({
    docId: doc.id,
    getDoc: () => doc,
    fetchImpl: async () => {
      calls++
      return Response.json({ error: '版本冲突' }, { status: 409 })
    },
  })
  saver.flushOnUnload()
  assert.equal(calls, 0)
  assert.equal(loadLocalDraft(doc.id)?.name, '未恢复草稿')
  doc.name = '编辑一'
  saver.markDirty()
  await saver.flush()
  doc.name = '编辑二'
  saver.markDirty()
  saver.flushOnUnload()
  assert.equal(calls, 1, '409 必须等待用户处理')
  assert.equal(loadLocalDraft(doc.id)?.name, '编辑二')
  clearLocalDraft(doc.id)
})
