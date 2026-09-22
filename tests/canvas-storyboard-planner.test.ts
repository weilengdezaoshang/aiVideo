import './dom.js'
import test from 'node:test'
import assert from 'node:assert/strict'
import { createStoryboardPlanner } from '../apps/web/canvas/flows/storyboard-planner.js'

const tick = () => new Promise((resolve) => setTimeout(resolve, 0))

test('规划刷新只查询原任务，完成后交付候选并清理恢复标识', async () => {
  const original = globalThis.fetch
  const calls: string[] = []
  const key = 'gencanvas.storyboardJob.recovered-job'
  localStorage.setItem(
    key,
    JSON.stringify({ id: 'job', documentId: 'recovered-job', prompt: '广告', workflow: 'product' }),
  )
  globalThis.fetch = async (url, init) => {
    calls.push(`${init?.method || 'GET'} ${url}`)
    return new Response(
      JSON.stringify({
        job: {
          id: 'job',
          documentId: 'recovered-job',
          status: 'completed',
          storyboard: { version: 1, outline: '完成', shots: [] },
        },
      }),
    )
  }
  let outline = ''
  const planner = createStoryboardPlanner('recovered-job', (state) => {
    outline = state.storyboard?.outline || outline
  })
  try {
    await tick()
    assert.deepEqual(calls, ['GET /api/storyboards/jobs/job'])
    assert.equal(outline, '完成')
    assert.equal(planner.state.busy, false)
    assert.equal(localStorage.getItem(key), null)
  } finally {
    planner.dispose()
    globalThis.fetch = original
  }
})

test('未查到提交时人工确认复用原标识与需求，关闭页面不发送取消', async () => {
  const original = globalThis.fetch
  const posts: Record<string, unknown>[] = []
  const methods: string[] = []
  globalThis.fetch = async (_url, init) => {
    methods.push(init?.method || 'GET')
    if (init?.method === 'POST') {
      const payload = JSON.parse(String(init.body)) as Record<string, unknown>
      posts.push(payload)
      if (posts.length === 1) {
        throw new Error('网络中断')
      }
      return new Response(JSON.stringify({ job: { ...payload, status: 'queued' } }))
    }
    return new Response('{}', { status: 404 })
  }
  const planner = createStoryboardPlanner('uncertain-job', () => {})
  try {
    await planner.start('原始需求', 'product')
    await tick()
    assert.equal(planner.state.uncertain, true)
    await planner.start('编辑后的需求', 'story')
    assert.equal(posts.length, 2)
    assert.deepEqual(posts[0], posts[1])
    planner.dispose()
    assert.ok(!methods.includes('DELETE'))
  } finally {
    planner.dispose()
    globalThis.fetch = original
    localStorage.removeItem('gencanvas.storyboardJob.uncertain-job')
  }
})
