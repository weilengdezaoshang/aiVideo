import test from 'node:test'
import assert from 'node:assert/strict'
import { createAgentApi } from '../apps/web/canvas/agent/api.js'

test('Host 模式初始化并发请求只加载一次目录，禁止浏览器选择模型', async () => {
  const calls: { path: string; body: unknown }[] = []
  const fetcher: typeof fetch = async (input, init) => {
    const path = String(input)
    calls.push({ path, body: init?.body ? (JSON.parse(String(init.body)) as unknown) : null })
    return new Response(
      JSON.stringify(
        path === '/api/agent/models'
          ? { hostEnabled: true, models: [], defaultModelId: 'host' }
          : { run: { id: 'run' }, runs: [] },
      ),
      { status: 200 },
    )
  }
  const api = createAgentApi(fetcher)
  await Promise.all([api.models(), api.snapshot('doc'), api.get('run')])
  await api.create('doc', {
    clientRequestId: 'request',
    message: '生成四方向图片',
    modelId: 'secret-model',
    baseRevision: 3,
    selectedObjectIds: ['node'],
    supersedeRunId: 'old',
  })
  await api.stop('run')
  await api.confirm?.('run', 'confirm', true)
  await api.final?.('run', 'final', 4)
  await api.retry?.('run', 'retry', 'images', 2)
  assert.equal(calls.filter((x) => x.path === '/api/agent/models').length, 1)
  assert.equal(api.eventPath?.('doc'), '/api/documents/doc/host/events')
  assert.ok(calls.some((x) => x.path === '/api/host/runs/run'))
  const body = calls.find((x) => x.path === '/api/host/turn')?.body
  assert.deepEqual(body, {
    clientRequestId: 'request',
    documentId: 'doc',
    message: '生成四方向图片',
    baseRevision: 3,
    selectedObjectIds: ['node'],
    supersedeRunId: 'old',
  })
  assert.deepEqual(calls.find((x) => x.path.endsWith('/retry'))?.body, {
    clientRequestId: 'retry',
    stepId: 'images',
    index: 2,
  })
  assert.ok(calls.some((x) => x.path.endsWith('/cancel')))
})
