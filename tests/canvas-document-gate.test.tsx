import './dom.js'
import test, { afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { CanvasDocumentGate } from '../apps/web/canvas/document-gate.js'
import { loadLocalDraft, writeLocalDraft } from '../apps/web/canvas/state/document-api.js'
import type { CanvasDocData } from '../apps/web/canvas/state/doc-store.js'

const originalFetch = globalThis.fetch
afterEach(() => {
  cleanup()
  globalThis.fetch = originalFetch
  localStorage.clear()
})
const server: CanvasDocData = {
  id: 'recovery',
  name: '服务端',
  objects: {},
  order: [],
  revision: 9,
}

function openProbe() {
  window.history.replaceState(null, '', '/canvas?doc=recovery')
  return render(
    <CanvasDocumentGate>
      {(doc, restored) => <output>{JSON.stringify({ doc, restored })}</output>}
    </CanvasDocumentGate>,
  )
}

test('存在草稿时不挂载编辑器或卸载保存，选择恢复后以服务器最新 revision 保存', async () => {
  const draft: CanvasDocData = {
    ...server,
    revision: 2,
    generations: {
      g: {
        id: 'g',
        operationId: 'request',
        status: 'uncertain',
        spec: { mode: 'text-to-image', params: {} },
        version: 1,
      },
    },
  }
  writeLocalDraft(server.id, draft)
  globalThis.fetch = async () => Response.json({ document: server })
  const first = openProbe()
  await screen.findByRole('dialog')
  assert.equal(screen.queryByRole('status'), null, '未选择时不启动编辑器')
  window.dispatchEvent(new Event('beforeunload'))
  first.unmount()
  assert.deepEqual(loadLocalDraft(server.id), draft)
  openProbe()
  fireEvent.click(await screen.findByRole('button', { name: '恢复本机草稿' }))
  const ready = JSON.parse(screen.getByRole('status').textContent || '{}') as {
    doc: CanvasDocData
    restored: boolean
  }
  assert.equal(ready.restored, true)
  assert.equal(ready.doc.revision, 9)
  assert.equal(ready.doc.generations?.g.status, 'uncertain')
  assert.deepEqual(loadLocalDraft(server.id), draft, '服务器确认前保留恢复副本')
})

test('读取失败保留草稿；重试后允许显式放弃', async () => {
  writeLocalDraft(server.id, { ...server, name: '本机' })
  let offline = true
  globalThis.fetch = async () => {
    if (offline) {
      throw new Error('连接失败')
    }
    return Response.json({ document: server })
  }
  openProbe()
  await screen.findByRole('alert')
  assert.equal(loadLocalDraft(server.id)?.name, '本机')
  offline = false
  fireEvent.click(screen.getByRole('button', { name: '重试读取' }))
  fireEvent.click(await screen.findByRole('button', { name: '放弃草稿，打开服务器版本' }))
  assert.equal(loadLocalDraft(server.id), null)
  assert.match(screen.getByRole('status').textContent || '', /服务端/)
})
