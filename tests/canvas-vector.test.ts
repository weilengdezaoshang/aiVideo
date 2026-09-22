// 矢量化引擎封装:参数映射、引擎就绪判定、串行队列与取消语义。
import './dom.js'
import test from 'node:test'
import assert from 'node:assert/strict'
import { defaultVectorParams, vectorEngineReady, vectorize } from '../apps/web/canvas/vector.js'

test('defaultVectorParams 是 S12 滑杆的中位档', () => {
  assert.deepEqual(defaultVectorParams(), { colors: 12, detail: 6, simplify: 8 })
})

test('vectorEngineReady 依赖 window.ImageTracer', () => {
  assert.equal(vectorEngineReady(), false)
  ;(window as unknown as Record<string, unknown>).ImageTracer = {}
  try {
    assert.equal(vectorEngineReady(), true)
  } finally {
    delete (window as unknown as Record<string, unknown>).ImageTracer
  }
})

test('引擎未加载时 vectorize 给出明确错误', async () => {
  await assert.rejects(vectorize({ image: new window.Image() }), /矢量引擎尚未加载完成/)
})

test('开始前已取消的请求抛出 AbortError,不进入队列执行', async () => {
  const controller = new AbortController()
  controller.abort()
  await assert.rejects(
    vectorize({ image: new window.Image(), signal: controller.signal }),
    (err: unknown) => err instanceof DOMException && err.name === 'AbortError',
  )
})
