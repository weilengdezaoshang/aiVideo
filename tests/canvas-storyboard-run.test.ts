import './dom.js'
import test from 'node:test'
import assert from 'node:assert/strict'
import { createStoryboardRun } from '../apps/web/canvas/flows/storyboard-run.js'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'
import { createNode, defaultDraft } from '../apps/web/canvas/state/node-model.js'
import { newShot } from '../apps/web/canvas/state/storyboard.js'

/* eslint-disable @typescript-eslint/no-explicit-any -- 测试桩使用宽类型 */

type FakeJob = {
  id: string
  status: string
  documentId: string
  requestId: string
  clientRef: string
  error?: string
  images?: any[]
}

/** 模拟服务端执行语义：同标识幂等、项目互斥、重试/对账/取消归属。 */
function createServer(docId: string) {
  const calls: { method: string; url: string; body?: any }[] = []
  const submissions: { clientRef: string; requestId: string }[] = []
  const cancelled: string[] = []
  const resumes: string[] = []
  const runs: Record<string, any> = {}
  const jobs: Record<string, FakeJob> = {}
  const state = { loseNextResponse: false }

  const json = (status: number, payload: unknown) =>
    new Response(JSON.stringify(payload), { status })

  function submitStep(run: any, index: number) {
    const step = run.steps[index]
    if (!step || step.jobId) {
      return
    }
    const job: FakeJob = {
      id: `job-${submissions.length}`,
      status: 'running',
      documentId: docId,
      requestId: step.request.requestId,
      clientRef: step.request.clientRef,
    }
    submissions.push({ clientRef: step.request.clientRef, requestId: step.request.requestId })
    jobs[job.id] = job
    step.jobId = job.id
    run.message = `正在生成第 ${index + 1} / ${run.steps.length} 镜`
  }

  function images(job: FakeJob) {
    return [
      {
        id: `img-${job.id}`,
        url: `/images/${job.id}.png`,
        file: `${job.id}.png`,
        params: { kind: 'image', width: 64, height: 64 },
      },
    ]
  }

  function advance(run: any) {
    if (run.status !== 'running') {
      return
    }
    run.index++
    if (run.index < run.steps.length) {
      submitStep(run, run.index)
    } else {
      run.status = 'completed'
      run.message = '全部镜头已生成'
    }
  }

  /** 测试驱动执行器循环：终结一个任务并按语义推进。 */
  function settle(runId: string, jobId: string, status: string, error?: string) {
    const run = runs[runId]
    const job = jobs[jobId]
    job.status = status
    if (error) {
      job.error = error
    }
    if (status === 'completed') {
      job.images = images(job)
    }
    if (!run || run.status !== 'running') {
      return
    }
    if (status === 'completed') {
      advance(run)
    } else if (status === 'failed' || status === 'unknown') {
      run.status = 'paused'
      run.message = '当前镜头失败或结果未知，请重试该镜头'
    }
  }

  function snapshot(run: any) {
    return {
      ...structuredClone(run),
      jobs: run.steps
        .map((step: any) => jobs[step.jobId])
        .filter(Boolean)
        .map((job: FakeJob) => structuredClone(job)),
    }
  }

  async function handler(input: any, init?: any): Promise<Response> {
    const method = init?.method || 'GET'
    const url = String(input)
    calls.push({ method, url, body: init?.body ? JSON.parse(init.body) : undefined })
    if (method === 'POST' && url === '/api/storyboards/runs') {
      const payload = JSON.parse(init.body)
      if (runs[payload.id]) {
        return json(202, { run: snapshot(runs[payload.id]) })
      }
      if (
        Object.values(runs).some(
          (run: any) => run.documentId === docId && ['running', 'paused'].includes(run.status),
        )
      ) {
        return json(409, { error: '当前项目已有执行记录，请继续或取消该记录' })
      }
      const run = {
        id: payload.id,
        documentId: payload.documentId,
        steps: structuredClone(payload.steps),
        index: 0,
        status: 'running',
        message: '开始顺序生成',
      }
      runs[run.id] = run
      submitStep(run, 0)
      return json(202, { run: snapshot(run) })
    }
    let match = url.match(/^\/api\/storyboards\/runs\/([0-9a-f-]+)$/)
    if (match && method === 'GET') {
      const run = runs[match[1]]
      return run ? json(200, { run: snapshot(run) }) : json(404, { error: '执行记录不存在' })
    }
    match = url.match(/^\/api\/storyboards\/runs\/([0-9a-f-]+)\/(\w+)$/)
    if (match && method === 'POST') {
      const run = runs[match[1]]
      const action = match[2]
      if (!run) {
        return json(404, { error: '执行记录不存在' })
      }
      if (action === 'pause') {
        run.status = 'paused'
        run.message = '已暂停，已提交镜头可继续完成'
      } else if (action === 'resume') {
        run.status = 'running'
        run.message = '继续执行'
        submitStep(run, run.index)
      } else if (action === 'cancel') {
        run.status = 'cancelled'
        run.message = '已取消后续执行'
      } else if (action === 'retry') {
        const step = run.steps[run.index]
        const job = step.jobId ? jobs[step.jobId] : undefined
        if (job?.status === 'unknown') {
          resumes.push(job.id)
          job.status = 'completed'
          job.images = images(job)
          run.status = 'running'
          run.message = '正在对账上游任务，未重新调用模型'
          advance(run)
        } else {
          step.attempts = [
            ...(step.attempts || []),
            { requestId: step.request.requestId, jobId: step.jobId, status: job?.status || null },
          ]
          step.request = { ...step.request, requestId: crypto.randomUUID() }
          step.jobId = null
          run.status = 'running'
          run.message = '重试当前镜头；旧尝试与结果已保留'
          submitStep(run, run.index)
        }
      }
      return json(200, { run: snapshot(run) })
    }
    if (url.startsWith('/api/storyboards/runs?')) {
      const query = new URLSearchParams(url.split('?')[1])
      const list = Object.values(runs)
        .filter((run: any) => run.documentId === query.get('documentId'))
        .map((run: any) => ({
          id: run.id,
          documentId: run.documentId,
          status: run.status,
          index: run.index,
          message: run.message,
        }))
        .reverse()
      return json(200, { runs: list })
    }
    return json(404, { error: '未找到' })
  }

  return { calls, submissions, cancelled, resumes, runs, jobs, state, settle, handler }
}

function installFetch(server: ReturnType<typeof createServer>) {
  const original = globalThis.fetch
  globalThis.fetch = (async (input: any, init?: any) => {
    if (
      server.state.loseNextResponse &&
      init?.method === 'POST' &&
      String(input) === '/api/storyboards/runs'
    ) {
      server.state.loseNextResponse = false
      await server.handler(input, init)
      throw new TypeError('模拟网络中断')
    }
    return server.handler(input, init)
  }) as typeof fetch
  return () => {
    globalThis.fetch = original
  }
}

function fixture(docId: string) {
  const nodes = [
    createNode('image', { x: 0, y: 0 }, '一'),
    createNode('image', { x: 400, y: 0 }, '二'),
  ]
  for (const [index, node] of nodes.entries()) {
    node.nodeDraft = { ...defaultDraft('image'), prompt: `镜头${index + 1}` }
  }
  const store = createDocStore({
    id: docId,
    name: 'doc',
    objects: Object.fromEntries(nodes.map((node) => [node.id, node])),
    order: nodes.map((node) => node.id),
    storyboard: {
      version: 1,
      outline: '',
      shots: nodes.map((node) => ({ ...newShot(), nodeId: node.id })),
    },
  })
  return { store, nodes }
}

function makeController(store: any, _server?: ReturnType<typeof createServer>) {
  return createStoryboardRun({
    documentId: store.doc.id,
    store,
    ensureSaved: async () => {},
    prepareNode: async (id: string) => structuredClone(store.doc.objects[id].nodeDraft),
    changed: () => {},
    pollMs: 10,
  })
}

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

async function waitFor(cond: () => boolean, timeout = 3000) {
  const start = Date.now()
  while (!cond()) {
    if (Date.now() - start > timeout) {
      throw new Error('等待超时')
    }
    await new Promise((resolve) => setTimeout(resolve, 5))
  }
}

test('两个镜头按顺序提交，结果回填后自动装配一次，撤销后不再追加', async () => {
  localStorage.clear()
  const server = createServer('run-e2e')
  const restore = installFetch(server)
  const { store, nodes } = fixture('run-e2e')
  const controller = makeController(store, server)
  try {
    await wait(10)
    await controller.start()
    const runId = controller.state.run!.id
    assert.equal(controller.state.run!.status, 'running')
    assert.equal(nodes[0].nodeRun?.runId, runId)
    assert.equal(nodes[1].nodeRun?.runId, runId)
    const posts = server.calls.filter(
      (call) => call.method === 'POST' && call.url === '/api/storyboards/runs',
    )
    assert.equal(posts.length, 1)
    assert.equal(posts[0].body.steps.length, 2)

    server.settle(runId, server.runs[runId].steps[0].jobId, 'completed')
    await waitFor(() => !!nodes[0].src)
    assert.equal(nodes[0].nodeRun, undefined)
    server.settle(runId, server.runs[runId].steps[1].jobId, 'completed')
    await waitFor(() => controller.state.run?.status === 'completed')
    await waitFor(() => (store.doc.timeline?.clips.length || 0) === 2)
    assert.ok(store.doc.timeline!.clips.every((clip) => clip.source.runId === runId))
    assert.equal(store.doc.timeline!.clips[0].subtitle, store.doc.storyboard!.shots[0].dialogue)

    // 重复轮询与重复结果不会重复装配
    await wait(40)
    assert.equal(store.doc.timeline!.clips.length, 2)
    // 整批撤销一次可撤销；后台轮询与刷新后的恢复都不得重新追加
    store.undo()
    assert.equal(store.doc.timeline, undefined)
    await wait(40)
    assert.equal(store.doc.timeline, undefined)
    controller.dispose()
    const refreshed = makeController(store, server)
    await waitFor(() => refreshed.state.run?.status === 'completed')
    await wait(40)
    assert.equal(store.doc.timeline, undefined, '刷新后撤销不可被自动装配覆盖')
    refreshed.dispose()
  } finally {
    controller.dispose()
    restore()
    localStorage.clear()
  }
})

test('页面关闭后服务端继续执行，刷新后只查询不重复提交', async () => {
  localStorage.clear()
  const server = createServer('run-recovery')
  const restore = installFetch(server)
  const { store, nodes } = fixture('run-recovery')
  const first = makeController(store, server)
  try {
    await wait(10)
    await first.start()
    const runId = first.state.run!.id
    first.dispose()
    const postsBefore = server.calls.filter(
      (call) => call.method === 'POST' && call.url === '/api/storyboards/runs',
    ).length
    server.settle(runId, server.runs[runId].steps[0].jobId, 'completed')
    const second = makeController(store, server)
    await waitFor(() => second.state.run?.id === runId)
    const postsAfter = server.calls.filter(
      (call) => call.method === 'POST' && call.url === '/api/storyboards/runs',
    ).length
    assert.equal(postsAfter, postsBefore)
    await waitFor(() => !!nodes[0].src)
    assert.equal(nodes[1].nodeRun?.runId, runId)
    second.dispose()
  } finally {
    restore()
    localStorage.clear()
  }
})

test('提交响应丢失后确认复用同一执行标识，不重复提交', async () => {
  localStorage.clear()
  const server = createServer('run-lost')
  const restore = installFetch(server)
  const { store } = fixture('run-lost')
  const controller = makeController(store, server)
  try {
    await new Promise((resolve) => setTimeout(resolve, 10))
    server.state.loseNextResponse = true
    await controller.start()
    assert.equal(controller.state.uncertain, true)
    const attempt = JSON.parse(
      localStorage.getItem('gencanvas.storyboardRunStart.run-lost') as string,
    )
    assert.equal(attempt.id, server.runs[attempt.id].id)
    const posts = () =>
      server.calls.filter((call) => call.method === 'POST' && call.url === '/api/storyboards/runs')
    assert.equal(posts().length, 1)
    const submissionsBefore = server.submissions.length
    await controller.confirmStart()
    assert.equal(posts().length, 2)
    assert.deepEqual(posts()[1].body, posts()[0].body)
    assert.equal(controller.state.run?.id, attempt.id)
    assert.equal(controller.state.uncertain, false)
    assert.equal(server.submissions.length, submissionsBefore)
    assert.equal(server.runs[attempt.id].steps.length, 2)
  } finally {
    controller.dispose()
    restore()
    localStorage.clear()
  }
})

test('多标签页不能启动重复执行，第二个标签页接管同一执行', async () => {
  localStorage.clear()
  const server = createServer('run-tabs')
  const restore = installFetch(server)
  const { store } = fixture('run-tabs')
  const tabA = makeController(store, server)
  const tabB = makeController(store, server)
  try {
    await new Promise((resolve) => setTimeout(resolve, 10))
    await tabA.start()
    const runId = tabA.state.run!.id
    await assert.rejects(() => tabB.start(), /已有执行记录/)
    assert.equal(tabB.state.run?.id, runId)
    assert.equal(Object.keys(server.runs).length, 1)
    const posts = server.calls.filter(
      (call) => call.method === 'POST' && call.url === '/api/storyboards/runs',
    )
    assert.equal(posts.length, 1, '第二个标签页不得再提交执行')
  } finally {
    tabA.dispose()
    tabB.dispose()
    restore()
    localStorage.clear()
  }
})

test('暂停后恢复继续推进；第二镜失败只重试第二镜并保留旧尝试', async () => {
  localStorage.clear()
  const server = createServer('run-retry')
  const restore = installFetch(server)
  const { store, nodes } = fixture('run-retry')
  const controller = makeController(store, server)
  try {
    await wait(10)
    await controller.start()
    const runId = controller.state.run!.id
    await controller.control('pause')
    assert.equal(controller.state.run?.status, 'paused')
    await controller.control('resume')
    assert.equal(controller.state.run?.status, 'running')
    const run = server.runs[runId]
    server.settle(runId, run.steps[0].jobId, 'completed')
    await waitFor(() => !!nodes[0].src)
    const secondRequestId = run.steps[1].request.requestId
    server.settle(runId, run.steps[1].jobId, 'failed', '模型超时')
    await waitFor(() => controller.state.run?.status === 'paused')
    assert.ok(controller.canRetryCurrent())
    await controller.control('retry')
    const step = run.steps[1]
    assert.notEqual(step.request.requestId, secondRequestId)
    assert.equal(step.attempts?.length, 1)
    assert.equal(step.attempts![0].requestId, secondRequestId)
    assert.equal(
      server.submissions.filter((item) => item.clientRef === nodes[0].id).length,
      1,
      '第一镜不得重试',
    )
    assert.equal(server.submissions.at(-1)!.clientRef, nodes[1].id)
    server.settle(runId, step.jobId, 'completed')
    await waitFor(() => controller.state.run?.status === 'completed')
    assert.ok(nodes[1].src)
  } finally {
    controller.dispose()
    restore()
    localStorage.clear()
  }
})

test('unknown 结果重试只对账上游，不重新调用模型', async () => {
  localStorage.clear()
  const server = createServer('run-unknown')
  const restore = installFetch(server)
  const { store } = fixture('run-unknown')
  const controller = makeController(store, server)
  try {
    await wait(10)
    await controller.start()
    const runId = controller.state.run!.id
    const run = server.runs[runId]
    server.settle(runId, run.steps[0].jobId, 'completed')
    await waitFor(() => run.index === 1)
    server.settle(runId, run.steps[1].jobId, 'unknown')
    await waitFor(() => controller.state.run?.status === 'paused')
    const submissionsBefore = server.submissions.length
    await controller.control('retry')
    assert.equal(server.submissions.length, submissionsBefore, 'unknown 不得重新提交')
    assert.deepEqual(server.resumes, [run.steps[1].jobId])
    assert.equal(controller.state.run?.status, 'completed')
  } finally {
    controller.dispose()
    restore()
    localStorage.clear()
  }
})

test('参数被修改后迟到结果不覆盖节点，保留为可查看的历史产物', async () => {
  localStorage.clear()
  const server = createServer('run-late')
  const restore = installFetch(server)
  const { store, nodes } = fixture('run-late')
  const first = makeController(store, server)
  try {
    await wait(10)
    await first.start()
    const runId = first.state.run!.id
    first.dispose()
    const run = server.runs[runId]
    server.settle(runId, run.steps[0].jobId, 'completed')
    server.settle(runId, run.steps[1].jobId, 'completed')
    store.mutateTransient(() => {
      nodes[0].nodeDraft!.prompt = '被用户修改的画面'
    })
    const second = makeController(store, server)
    await waitFor(() => second.state.run?.status === 'completed')
    await wait(40)
    assert.equal(nodes[0].src, undefined, '修改后的节点不得被迟到结果覆盖')
    assert.ok(nodes[1].src)
    const unmatched = second.state.unmatched
    assert.equal(unmatched.length, 1)
    assert.match(unmatched[0].reason, /参数已修改/)
    assert.ok(unmatched[0].url)
    assert.equal(unmatched[0].index, 0)
    second.dispose()
  } finally {
    restore()
    localStorage.clear()
  }
})

test('镜头切换版本后，迟到结果不覆盖新选用节点', async () => {
  localStorage.clear()
  const server = createServer('run-version')
  const restore = installFetch(server)
  const { store, nodes } = fixture('run-version')
  const first = makeController(store, server)
  try {
    await wait(10)
    await first.start()
    const runId = first.state.run!.id
    first.dispose()
    server.settle(runId, server.runs[runId].steps[0].jobId, 'completed')
    server.settle(runId, server.runs[runId].steps[1].jobId, 'completed')
    const replacement = createNode('image', { x: 800, y: 0 }, '新版本')
    replacement.nodeDraft = { ...defaultDraft('image'), prompt: '镜头1 新方向' }
    store.apply({ type: 'addObjects', objects: [replacement] })
    const board = structuredClone(store.doc.storyboard)!
    board.shots[0].nodeId = replacement.id
    store.apply({ type: 'setStoryboard', before: store.doc.storyboard, after: board })
    const second = makeController(store, server)
    await waitFor(() => second.state.run?.status === 'completed')
    await wait(40)
    assert.equal(nodes[0].src, undefined)
    assert.equal(replacement.src, undefined)
    assert.ok(nodes[1].src)
    const unmatched = second.state.unmatched
    assert.equal(unmatched.length, 1)
    assert.match(unmatched[0].reason, /版本/)
    second.dispose()
  } finally {
    restore()
    localStorage.clear()
  }
})

test('执行期间时间线被手动修改时展示待装配，手动追加保留用户编辑', async () => {
  localStorage.clear()
  const server = createServer('run-edit')
  const restore = installFetch(server)
  const { store } = fixture('run-edit')
  const controller = makeController(store, server)
  try {
    await wait(10)
    await controller.start()
    const runId = controller.state.run!.id
    const manualClip = {
      id: 'manual-clip',
      name: '手动剪辑',
      source: {
        nodeId: 'ext',
        kind: 'image' as const,
        url: '/images/ext.png',
        durationFrames: 3000,
      },
      inFrame: 0,
      outFrame: 150,
    }
    store.apply({
      type: 'setTimeline',
      before: undefined,
      after: { version: 1, fps: 30, width: 1920, height: 1080, clips: [manualClip] },
    })
    const run = server.runs[runId]
    server.settle(runId, run.steps[0].jobId, 'completed')
    server.settle(runId, run.steps[1].jobId, 'completed')
    await waitFor(() => controller.state.run?.status === 'completed')
    await wait(40)
    assert.equal(controller.state.assemblePending, true)
    assert.equal(store.doc.timeline!.clips.length, 1, '不得自动覆盖手动修改')
    await controller.assembleNow()
    const clips = store.doc.timeline!.clips
    assert.equal(clips.length, 3)
    assert.equal(clips[0].id, 'manual-clip')
    assert.ok(clips.slice(1).every((clip) => clip.source.runId === runId))
    store.undo()
    assert.equal(store.doc.timeline!.clips.length, 1)
    await wait(40)
    assert.equal(store.doc.timeline!.clips.length, 1, '撤销后轮询不得重新装配')
  } finally {
    controller.dispose()
    restore()
    localStorage.clear()
  }
})

test('取消执行后清除镜头归属标记，不重复轮询', async () => {
  localStorage.clear()
  const server = createServer('run-cancel')
  const restore = installFetch(server)
  const { store, nodes } = fixture('run-cancel')
  const controller = makeController(store, server)
  try {
    await wait(10)
    await controller.start()
    await controller.control('cancel')
    assert.equal(controller.state.run?.status, 'cancelled')
    assert.equal(nodes[0].nodeRun, undefined)
    assert.equal(nodes[1].nodeRun, undefined)
  } finally {
    controller.dispose()
    restore()
    localStorage.clear()
  }
})
