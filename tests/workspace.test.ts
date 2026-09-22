import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  CreationIntent,
  validateGeneration,
  type CanvasDocument,
} from '../apps/web/workspace/api.js'
import { defaultDraft, type Capabilities } from '../apps/web/canvas/state/node-model.js'
import { filterInspiration, inspiration } from '../apps/web/workspace/inspiration.js'

const cap = {
  supported: true,
  models: [{ id: 'mock', name: 'Mock' }],
  ratios: ['1:1', '16:9'],
  resolutions: [512, 1024],
  referenceLimit: 1,
  durations: [4],
}
const caps: Capabilities = { provider: 'mock', image: cap, video: cap }
const draft = { ...defaultDraft('image'), prompt: '海面上的帆船', model: 'mock' }
const response = (value: unknown) =>
  new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })

test('homepage validates provider models, references and video requirements', () => {
  assert.equal(validateGeneration('image', draft, caps), null)
  assert.match(validateGeneration('image', { ...draft, model: 'unknown' }, caps)!, /可用模型/)
  assert.match(validateGeneration('video', draft, caps)!, /首帧/)
  assert.match(validateGeneration('image', { ...draft, prompt: '  ' }, caps)!, /描述/)
  assert.match(
    validateGeneration(
      'image',
      { ...draft, references: [{ assetId: 'ref', ext: 'png', name: '参考' }] },
      { ...caps, image: { ...cap, referenceLimit: 0 } },
    )!,
    /参考图/,
  )
  assert.equal(
    validateGeneration('video', draft, {
      ...caps,
      video: { ...cap, supported: false, reason: '视频不可用' },
    }),
    '视频不可用',
  )
})

test('inspiration search intersects category and normalized prompt search', () => {
  assert.equal(filterInspiration(inspiration, '精选', '  帆船  ')[0].id, 'sea')
  assert.equal(filterInspiration(inspiration, '产品', '帆船').length, 0)
  assert.equal(
    filterInspiration(inspiration, '自然', '').every((item) => item.category === '自然'),
    true,
  )
})

function fixture(failure: 'create' | 'save' | 'generate') {
  let document: CanvasDocument = {
    id: 'doc-1',
    name: '新画布',
    revision: 1,
    updatedAt: '',
    objects: {},
    order: [],
  }
  let failed = false
  const keys: string[] = []
  const submissions: string[] = []
  let saves = 0
  const fetcher: typeof fetch = async (url, init) => {
    const path = String(url)
    if (path === '/api/documents') {
      keys.push(new Headers(init?.headers).get('Idempotency-Key')!)
      if (failure === 'create' && !failed) {
        failed = true
        throw new TypeError('lost create response')
      }
      return response({ document })
    }
    if (path === '/api/documents/doc-1' && init?.method === 'POST') {
      document = { ...document, ...JSON.parse(String(init.body)), revision: document.revision + 1 }
      saves++
      if (failure === 'save' && !failed) {
        failed = true
        throw new TypeError('lost save response')
      }
      return response({ document })
    }
    if (path === '/api/documents/doc-1') {
      return response({ document })
    }
    if (path === '/api/generate') {
      submissions.push(String(init?.body))
      const body = JSON.parse(submissions.at(-1)!) as { clientRef: string; requestId: string }
      assert.equal(
        document.objects[body.clientRef].nodeRun?.requestId,
        body.requestId,
        'node persisted before dispatch',
      )
      if (failure === 'generate' && !failed) {
        failed = true
        throw new TypeError('lost submission response')
      }
      return response({ job: { id: 'job-1', status: 'queued' } })
    }
    throw new Error(`Unexpected path ${path}`)
  }
  return { fetcher, keys, submissions, saves: () => saves }
}
for (const step of ['create', 'save', 'generate'] as const) {
  test(`homepage retry recovers uncertain ${step} without another canvas or generation`, async () => {
    const mock = fixture(step)
    const intent = new CreationIntent('image', draft, mock.fetcher)
    await assert.rejects(intent.run())
    assert.equal(await intent.run(), 'doc-1')
    assert.equal(new Set(mock.keys).size, 1)
    assert.equal(mock.saves(), 1)
    assert.equal(new Set(mock.submissions).size, 1)
  })
}
test('navigation never creates documents; empty intent only creates on explicit run', async () => {
  let calls = 0
  const fetcher: typeof fetch = async () => {
    calls++
    return response({ document: { id: 'new-doc' } })
  }
  const intent = new CreationIntent(undefined, undefined, fetcher)
  assert.equal(calls, 0)
  assert.equal(await intent.run(), 'new-doc')
  assert.equal(calls, 1)
})
