// 文档 API:本机草稿安全网与打开/创建契约(PRD §3/§9)。
import './dom.js'
import test from 'node:test'
import assert from 'node:assert/strict'
import { createAutoSaver } from '../apps/web/canvas/state/autosave.js'
import {
  clearLocalDraft,
  createDocument,
  DocumentMissingError,
  draftMatchesDocument,
  loadLocalDraft,
  openDocument,
  renameDocument,
  resolveDraftOnOpen,
  writeLocalDraft,
} from '../apps/web/canvas/state/document-api.js'

const jsonResponse = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })

const canvasObj = (id: string, extra: Record<string, unknown> = {}) => ({
  id,
  kind: 'image' as const,
  x: 0,
  y: 0,
  width: 256,
  height: 256,
  ...extra,
})

test('本机草稿:写入后可读,clear 之后不可读', () => {
  const snapshot = { id: 'doc-1', name: '测试', objects: {}, order: [] }
  assert.equal(writeLocalDraft('doc-1', snapshot), true)
  assert.deepEqual(loadLocalDraft('doc-1')?.id, 'doc-1')
  clearLocalDraft('doc-1')
  assert.equal(loadLocalDraft('doc-1'), null)
  assert.equal(loadLocalDraft('doc-never'), null, '不存在的草稿返回 null')
})

test('GET 文档成功:草稿与服务器一致才清除,含未保存内容时保留', () => {
  const serverDoc = { id: 'doc-1', name: '画布', objects: {}, order: [], revision: 4 }
  // 无草稿:直接打开服务端文档
  assert.deepEqual(resolveDraftOnOpen(serverDoc, null), {
    action: 'open-server',
    clearDraft: false,
  })
  // 草稿内容服务端已包含(等价保存确认)→ 可清除
  assert.deepEqual(resolveDraftOnOpen(serverDoc, { ...serverDoc, revision: 3 }), {
    action: 'open-server',
    clearDraft: true,
  })
  // 草稿含未保存修改(如保存失败/冲突/beacon 未送达)→ 保留,由用户决定
  const dirtyDraft = {
    ...serverDoc,
    revision: 3,
    objects: { a: canvasObj('a', { src: '/images/img_1.png' }) },
    order: ['a'],
  }
  assert.deepEqual(resolveDraftOnOpen(serverDoc, dirtyDraft), {
    action: 'needs-choice',
    draft: dirtyDraft,
  })
})

test('draftMatchesDocument 忽略 revision/updatedAt,内容差异可检出', () => {
  const serverDoc = {
    id: 'doc-1',
    name: '画布',
    objects: { a: canvasObj('a', { src: '/images/img_1.png' }) },
    order: ['a'],
    revision: 4,
    updatedAt: '2026-09-12T10:00:00Z',
  }
  assert.equal(draftMatchesDocument({ ...serverDoc, revision: 3 }, serverDoc), true)
  assert.equal(
    draftMatchesDocument(
      { ...serverDoc, revision: 3, objects: { ...serverDoc.objects, b: canvasObj('b') } },
      serverDoc,
    ),
    false,
  )
})

test('保存成功(保存确认)后清除本机草稿', async () => {
  const doc = { id: 'doc-save-ok', name: '画布', objects: {}, order: [], revision: 3 }
  const original = globalThis.fetch
  globalThis.fetch = (async () => jsonResponse({ document: { revision: 4 } })) as typeof fetch
  try {
    const saver = createAutoSaver({ docId: doc.id, getDoc: () => doc, debounceMs: 5 })
    saver.markDirty()
    await new Promise((r) => setTimeout(r, 40))
    assert.equal(loadLocalDraft(doc.id), null, '保存确认后草稿被清除')
  } finally {
    globalThis.fetch = original
    clearLocalDraft(doc.id)
  }
})

test('保存失败后保留本机草稿,刷新后仍可恢复', async () => {
  const doc = {
    id: 'doc-save-fail',
    name: '画布',
    objects: { a: canvasObj('a') },
    order: ['a'],
    revision: 3,
  }
  const original = globalThis.fetch
  globalThis.fetch = (async () => jsonResponse({ error: '服务器错误' }, 500)) as typeof fetch
  try {
    const saver = createAutoSaver({ docId: doc.id, getDoc: () => doc, debounceMs: 5 })
    saver.markDirty()
    await new Promise((r) => setTimeout(r, 40))
    const draft = loadLocalDraft(doc.id)
    assert.ok(draft, '保存失败后草稿仍在')
    assert.deepEqual(draft?.order, ['a'])
  } finally {
    globalThis.fetch = original
    clearLocalDraft(doc.id)
  }
})

test('?doc= 严格打开:404 抛 DocumentMissingError,不新建兜底', async () => {
  window.history.replaceState(null, '', '/canvas?doc=missing-1')
  const original = globalThis.fetch
  globalThis.fetch = (async () => jsonResponse({ error: 'not found' }, 404)) as typeof fetch
  try {
    await assert.rejects(openDocument(), DocumentMissingError)
  } finally {
    globalThis.fetch = original
  }
})

test('?doc= 打开成功:记住文档 id 并返回文档', async () => {
  window.history.replaceState(null, '', '/canvas?doc=doc-9')
  const original = globalThis.fetch
  const doc = { id: 'doc-9', name: '海岸', objects: {}, order: [], revision: 3 }
  globalThis.fetch = (async () => jsonResponse({ document: doc })) as typeof fetch
  try {
    const opened = await openDocument()
    assert.equal(opened.id, 'doc-9')
    assert.equal(localStorage.getItem('gencanvas.docId'), 'doc-9')
  } finally {
    globalThis.fetch = original
    localStorage.removeItem('gencanvas.docId')
  }
})

test('createDocument 携带幂等键;服务端错误带状态上抛', async () => {
  localStorage.removeItem('gencanvas.docId')
  const original = globalThis.fetch
  const headers: Record<string, string> = {}
  globalThis.fetch = (async (_url: string, init?: RequestInit) => {
    Object.assign(headers, init?.headers)
    return jsonResponse({ document: { id: 'doc-new', name: '未命名画布', objects: {}, order: [] } })
  }) as typeof fetch
  try {
    const doc = await createDocument({ idempotencyKey: 'web-fixed-key' })
    assert.equal(doc.id, 'doc-new')
    assert.equal(headers['Idempotency-Key'], 'web-fixed-key')
    assert.equal(localStorage.getItem('gencanvas.docId'), 'doc-new')
  } finally {
    globalThis.fetch = original
    localStorage.removeItem('gencanvas.docId')
  }
})

test('renameDocument 只提交名称字段', async () => {
  const original = globalThis.fetch
  let body = ''
  globalThis.fetch = (async (_url: string, init?: RequestInit) => {
    body = String(init?.body)
    return jsonResponse({ document: { id: 'doc-1', name: '新名字', objects: {}, order: [] } })
  }) as typeof fetch
  try {
    const doc = await renameDocument('doc-1', '新名字')
    assert.equal(doc.name, '新名字')
    assert.deepEqual(JSON.parse(body), { name: '新名字' })
  } finally {
    globalThis.fetch = original
  }
})
