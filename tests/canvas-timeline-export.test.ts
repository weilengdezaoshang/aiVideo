import './dom.js'
import test from 'node:test'
import assert from 'node:assert/strict'
import { fireEvent, screen, waitFor } from '@testing-library/dom'
import { createTimelineExport } from '../apps/web/canvas/flows/timeline-export.js'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'
import { emptyTimeline } from '../apps/web/canvas/state/timeline.js'

test('导出提交当前快照并显示实际任务的下载链接', async () => {
  document.body.innerHTML =
    '<section><header><button data-action="close">收起</button></header></section>'
  const original = globalThis.fetch
  const store = createDocStore({
    id: 'export-test',
    name: '测试',
    objects: {},
    order: [],
    timeline: {
      ...emptyTimeline(),
      clips: [
        {
          id: 'clip',
          name: '镜头',
          source: {
            nodeId: 'node',
            kind: 'image',
            url: '/images/img_12345678.png',
            durationFrames: 30,
          },
          inFrame: 0,
          outFrame: 30,
        },
      ],
    },
  })
  let captured: { documentId: string; requestId: string; timeline: unknown } | undefined
  globalThis.fetch = (async (_url, init) => {
    captured = JSON.parse(String(init?.body)) as typeof captured
    return new Response(
      JSON.stringify({ export: { id: 'server-job-id', status: 'completed', progress: 100 } }),
      { status: 202, headers: { 'Content-Type': 'application/json' } },
    )
  }) as typeof fetch
  const view = createTimelineExport(document.querySelector('section')!, store)
  try {
    fireEvent.click(screen.getByRole('button', { name: '导出 MP4' }))
    await waitFor(() => assert.ok(screen.getByRole('link', { name: '下载成片' })))
    assert.equal(captured?.documentId, 'export-test')
    assert.deepEqual(captured?.timeline, store.doc.timeline)
    assert.equal(
      screen.getByRole('link', { name: '下载成片' }).getAttribute('href'),
      '/api/exports/server-job-id/download',
    )
  } finally {
    view.dispose()
    globalThis.fetch = original
    document.body.innerHTML = ''
  }
})

test('导出取消使用服务端任务ID，保存失败不会发送导出请求', async () => {
  document.body.innerHTML =
    '<section><header><button data-action="close">收起</button></header></section>'
  const store = createDocStore({
    id: 'doc',
    name: 'test',
    objects: {},
    order: [],
    timeline: {
      ...emptyTimeline(),
      clips: [
        {
          id: 'c',
          name: 'c',
          source: { nodeId: 'n', kind: 'image', url: '/api/assets/id/content', durationFrames: 30 },
          inFrame: 0,
          outFrame: 30,
        },
      ],
    },
  })
  const previous = globalThis.fetch
  const calls: { url: string; method: string }[] = []
  globalThis.fetch = (async (url, init) => {
    calls.push({ url: String(url), method: init?.method ?? 'GET' })
    return new Response(
      JSON.stringify({
        export: {
          id: 'server-id',
          status: init?.method === 'DELETE' ? 'cancelled' : 'queued',
          progress: 0,
        },
      }),
    )
  }) as typeof fetch
  let view = createTimelineExport(document.querySelector('section')!, store, {
    storageKey: 'test.cancel.export',
  })
  try {
    fireEvent.click(screen.getByRole('button', { name: '导出 MP4' }))
    await waitFor(() => assert.ok(screen.getByRole('button', { name: '取消导出' })))
    fireEvent.click(screen.getByRole('button', { name: '取消导出' }))
    await waitFor(() =>
      assert.ok(
        calls.some((call) => call.url === '/api/exports/server-id' && call.method === 'DELETE'),
      ),
    )
    view.dispose()
    calls.length = 0
    view = createTimelineExport(document.querySelector('section')!, store, {
      storageKey: 'test.failed.export',
      ensureSaved: async () => {
        throw new Error('save failed')
      },
    })
    fireEvent.click(screen.getByRole('button', { name: '导出 MP4' }))
    await waitFor(() => assert.match(document.body.textContent ?? '', /保存失败/))
    assert.equal(calls.length, 0)
  } finally {
    view.dispose()
    globalThis.fetch = previous
    localStorage.removeItem('test.cancel.export')
    localStorage.removeItem('test.failed.export')
    document.body.innerHTML = ''
  }
})
