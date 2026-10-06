import test from 'node:test'
import assert from 'node:assert/strict'
import {
  RecognitionOperation,
  createCanvasRecognition,
} from '../apps/web/canvas/flows/recognition-operation.js'

test('取消后即使响应迟到也不解析或应用，并只发送一次取消', async () => {
  const original = globalThis.fetch
  let resolve!: (response: Response) => void
  const calls: string[] = []
  globalThis.fetch = async (url, init) => {
    calls.push(`${init?.method} ${url}`)
    if (init?.method === 'DELETE') {
      return new Response('{}')
    }
    return new Promise<Response>((r) => {
      resolve = r
    })
  }
  try {
    const operation = new RecognitionOperation(() => true)
    const pending = operation.detect('source')
    operation.cancel()
    operation.cancel()
    resolve(new Response('not even valid JSON'))
    await assert.rejects(pending, { name: 'AbortError' })
    assert.equal(calls.filter((call) => call.startsWith('DELETE')).length, 1)
    assert.equal(operation.signal.aborted, true)
  } finally {
    globalThis.fetch = original
  }
})

test('解析响应期间源图替换使旧操作失效，新操作不受影响', async () => {
  const original = globalThis.fetch
  let matches = true
  let resolve!: (value: unknown) => void
  const old = new RecognitionOperation(() => matches)
  globalThis.fetch = async () =>
    ({
      ok: true,
      json: () =>
        new Promise((r) => {
          resolve = r
        }),
    }) as Response
  try {
    const pending = old.detect('source')
    await Promise.resolve()
    matches = false
    const next = new RecognitionOperation(() => true)
    resolve({ operationId: old.id, urls: { original: '/old.png' } })
    await assert.rejects(pending, { name: 'AbortError' })
    assert.equal(next.current(), true)
    assert.notEqual(next.id, old.id)
  } finally {
    globalThis.fetch = original
  }
})

test('只接受匹配的操作结果，完成后取消不会影响已提交任务', async () => {
  const original = globalThis.fetch
  const operation = new RecognitionOperation(() => true)
  let calls = 0
  globalThis.fetch = async () => {
    calls++
    return Response.json({ operationId: operation.id, urls: { original: '/mask.png' } })
  }
  try {
    assert.equal(await operation.detect('source'), '/mask.png')
    operation.complete()
    operation.cancel()
    assert.equal(calls, 1)
    assert.equal(operation.current(), false)
    const invalid = new RecognitionOperation(() => true)
    await assert.rejects(invalid.detect('source'), /不完整/)
  } finally {
    globalThis.fetch = original
  }
})

test('空选择、多选、切换工具和 Escape 同步取消识别，迟到结果不重开蒙版', async () => {
  const { createCanvasView } = await import('../apps/web/canvas/core/canvas-view.js')
  const { createDocStore } = await import('../apps/web/canvas/state/doc-store.js')
  const original = globalThis.fetch
  for (const leave of ['empty', 'other', 'multiple', 'pan', 'escape'] as const) {
    const store = createDocStore({
      id: 'recognition-view',
      name: '',
      order: ['source', 'other'],
      objects: {
        source: { id: 'source', kind: 'image', x: 0, y: 0, width: 100, height: 100 },
        other: { id: 'other', kind: 'image', x: 200, y: 0, width: 100, height: 100 },
      },
    })
    const view = createCanvasView({ store })
    view.select(['source'])
    view.activateTool('recognizing')
    let resolve!: (response: Response) => void
    let cancels = 0
    globalThis.fetch = async (_input, init) => {
      if (init?.method === 'DELETE') {
        cancels++
        return Response.json({})
      }
      return new Promise<Response>((done) => {
        resolve = done
      })
    }
    try {
      const raw = store.doc.objects.source
      const operation = createCanvasRecognition(
        view,
        'source',
        () => store.doc.objects.source === raw,
      )
      const pending = operation.detect('source')
      const rejected = assert.rejects(pending, { name: 'AbortError' })
      if (leave === 'empty') {
        view.select([])
      } else if (leave === 'other') {
        view.select(['other'])
      } else if (leave === 'multiple') {
        view.select(['source', 'other'])
      } else if (leave === 'pan') {
        view.activateTool('pan')
      } else {
        view.dispatchInput({
          type: 'key-down',
          key: 'Escape',
          code: 'Escape',
          modifiers: {
            shift: false,
            alt: false,
            ctrl: false,
            meta: false,
            space: false,
          },
        })
      }
      assert.equal(operation.signal.aborted, true, leave)
      assert.equal(cancels, 1, leave)
      const tool = view.getSnapshot().requestedTool
      resolve(Response.json({ operationId: operation.id, urls: { original: '/late.png' } }))
      await rejected
      assert.equal(view.getSnapshot().requestedTool, tool)
    } finally {
      view.destroy()
      globalThis.fetch = original
    }
  }
})
