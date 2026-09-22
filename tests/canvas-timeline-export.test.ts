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
      JSON.stringify({ export: { id: captured!.requestId, status: 'completed', progress: 100 } }),
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
      `/api/exports/${captured?.requestId}/download`,
    )
  } finally {
    view.dispose()
    globalThis.fetch = original
    document.body.innerHTML = ''
  }
})
