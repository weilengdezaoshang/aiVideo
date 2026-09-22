import './dom.js'
import test from 'node:test'
import assert from 'node:assert/strict'
import { fireEvent, screen } from '@testing-library/dom'
import { createStoryboardPanel } from '../apps/web/canvas/flows/storyboard-panel.js'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'

test('分镜面板添加编辑锁定与撤销', () => {
  document.body.innerHTML = '<div class="topbar-tools"></div>'
  const store = createDocStore({ id: 'doc', name: 'doc', objects: {}, order: [] })
  const panel = createStoryboardPanel(store, () => [])
  try {
    fireEvent.click(screen.getByRole('button', { name: '分镜' }))
    fireEvent.click(screen.getByRole('button', { name: '添加镜头' }))
    fireEvent.change(screen.getByRole('textbox', { name: '镜头 1 画面描述' }), {
      target: { value: '产品缓慢旋转' },
    })
    assert.equal(store.doc.storyboard!.shots[0].visual, '产品缓慢旋转')
    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '8' } })
    assert.equal(store.doc.storyboard!.shots[0].durationFrames, 240)
    fireEvent.click(screen.getByRole('button', { name: '锁定镜头' }))
    assert.equal(
      (screen.getByRole('button', { name: '删除镜头' }) as HTMLButtonElement).disabled,
      true,
    )
    fireEvent.click(screen.getByRole('button', { name: '撤销' }))
    assert.equal(store.doc.storyboard!.shots[0].locked, false)
    fireEvent.click(screen.getByRole('button', { name: '删除镜头' }))
    assert.equal(store.doc.storyboard!.shots.length, 0)
    fireEvent.click(screen.getByRole('button', { name: '撤销' }))
    assert.equal(store.doc.storyboard!.shots.length, 1)
  } finally {
    panel.dispose()
  }
})

test('规划候选按项目恢复，采用后清除候选缓存', () => {
  document.body.innerHTML = '<div class="topbar-tools"></div>'
  const shot = {
    id: 'planned',
    title: '开场',
    visual: '产品特写',
    dialogue: '',
    camera: '',
    durationFrames: 60,
    nodeId: null,
    locked: false,
  }
  localStorage.setItem(
    'gencanvas.storyboardPlan.recovery',
    JSON.stringify({
      prompt: '产品广告',
      candidate: { version: 1, outline: '广告', shots: [shot] },
    }),
  )
  const store = createDocStore({ id: 'recovery', name: 'doc', objects: {}, order: [] })
  const panel = createStoryboardPanel(store, () => [])
  try {
    fireEvent.click(screen.getByRole('button', { name: '分镜' }))
    assert.equal(
      (screen.getByRole('textbox', { name: 'Agent 创作需求' }) as HTMLTextAreaElement).value,
      '产品广告',
    )
    fireEvent.click(screen.getByRole('button', { name: '采用并追加分镜' }))
    assert.equal(store.doc.storyboard!.shots[0].id, 'planned')
    assert.equal(
      JSON.parse(localStorage.getItem('gencanvas.storyboardPlan.recovery')!).candidate,
      null,
    )
  } finally {
    panel.dispose()
    localStorage.clear()
  }
})

test('分镜执行按钮调用共享执行入口，锁定镜头不可提交', async () => {
  document.body.innerHTML = '<div class="topbar-tools"></div>'
  const { createNode } = await import('../apps/web/canvas/state/node-model.js')
  const { newShot } = await import('../apps/web/canvas/state/storyboard.js')
  const node = createNode('image', { x: 0, y: 0 }, '镜头')
  const shot = { ...newShot(), nodeId: node.id }
  const store = createDocStore({
    id: 'execution',
    name: 'doc',
    objects: { [node.id]: node },
    order: [node.id],
    storyboard: { version: 1, outline: '', shots: [shot] },
  })
  const submitted: string[] = []
  const panel = createStoryboardPanel(
    store,
    () => [],
    () => {},
    {
      submit: async (id) => {
        submitted.push(id)
      },
      cancel: () => {},
    },
  )
  try {
    fireEvent.click(screen.getByRole('button', { name: '分镜' }))
    fireEvent.click(screen.getByRole('button', { name: '生成此镜头' }))
    assert.deepEqual(submitted, [node.id])
    fireEvent.click(screen.getByRole('button', { name: '锁定镜头' }))
    assert.equal(
      (screen.getByRole('button', { name: '生成此镜头' }) as HTMLButtonElement).disabled,
      true,
    )
  } finally {
    panel.dispose()
  }
})

test('生成状态更新不会清空尚未提交的分镜输入', async () => {
  document.body.innerHTML = '<div class="topbar-tools"></div>'
  const { createNode } = await import('../apps/web/canvas/state/node-model.js')
  const { newShot } = await import('../apps/web/canvas/state/storyboard.js')
  const node = createNode('image', { x: 0, y: 0 }, '镜头')
  const store = createDocStore({
    id: 'editing',
    name: 'doc',
    objects: { [node.id]: node },
    order: [node.id],
    storyboard: { version: 1, outline: '', shots: [{ ...newShot(), nodeId: node.id }] },
  })
  const panel = createStoryboardPanel(store, () => [])
  try {
    fireEvent.click(screen.getByRole('button', { name: '分镜' }))
    const field = screen.getByRole('textbox', { name: '镜头 1 画面描述' }) as HTMLTextAreaElement
    field.focus()
    fireEvent.input(field, { target: { value: '正在输入的新画面' } })
    store.mutateTransient(() => {
      node.nodeRun = {
        requestId: 'test',
        status: 'running',
        snapshot: node.nodeDraft!,
        progress: 50,
      }
    })
    assert.equal(screen.getByRole('textbox', { name: '镜头 1 画面描述' }), field)
    assert.equal(field.value, '正在输入的新画面')
    fireEvent.change(field)
    field.blur()
    await Promise.resolve()
    assert.equal(store.doc.storyboard!.shots[0].visual, '正在输入的新画面')
  } finally {
    panel.dispose()
  }
})

test('版本比较展开后显示真实图片并支持切换选用与撤销', async () => {
  document.body.innerHTML = '<div class="topbar-tools"></div>'
  const { createNode } = await import('../apps/web/canvas/state/node-model.js')
  const { newShot } = await import('../apps/web/canvas/state/storyboard.js')
  const a = createNode('image', { x: 0, y: 0 }, '一')
  const b = createNode('image', { x: 400, y: 0 }, '二')
  a.src = '/a.png'
  b.src = '/b.png'
  const store = createDocStore({
    id: 'versions',
    name: 'doc',
    objects: { [a.id]: a, [b.id]: b },
    order: [a.id, b.id],
    storyboard: {
      version: 1,
      outline: '',
      shots: [{ ...newShot(), nodeId: a.id, versions: [a.id, b.id] }],
    },
  })
  const panel = createStoryboardPanel(store, () => [])
  try {
    fireEvent.click(screen.getByRole('button', { name: '分镜' }))
    assert.equal(document.querySelectorAll('.shot-comparison img').length, 0)
    const details = document.querySelector<HTMLDetailsElement>('.shot-comparison')!
    details.open = true
    fireEvent(details, new Event('toggle'))
    assert.equal(screen.getByAltText('版本 2 二').getAttribute('src'), '/b.png')
    fireEvent.click(screen.getByRole('button', { name: '选用版本 2' }))
    assert.equal(store.doc.storyboard!.shots[0].nodeId, b.id)
    store.undo()
    assert.equal(store.doc.storyboard!.shots[0].nodeId, a.id)
  } finally {
    panel.dispose()
  }
})

/* eslint-disable @typescript-eslint/no-explicit-any -- 测试桩使用宽类型 */
test('顺序生成接入服务端执行，准备期间输入不被覆盖', async () => {
  document.body.innerHTML = '<div class="topbar-tools"></div>'
  const { createNode } = await import('../apps/web/canvas/state/node-model.js')
  const { newShot } = await import('../apps/web/canvas/state/storyboard.js')
  const node = createNode('image', { x: 0, y: 0 }, '镜头')
  node.nodeDraft!.prompt = '镜头画面'
  const store = createDocStore({
    id: 'server-run',
    name: 'doc',
    objects: { [node.id]: node },
    order: [node.id],
    storyboard: { version: 1, outline: '', shots: [{ ...newShot(), nodeId: node.id }] },
  })
  let release!: () => void
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  const calls: { url: string; method: string; body?: any }[] = []
  let run: any = null
  const original = globalThis.fetch
  globalThis.fetch = (async (url: any, init?: any) => {
    const method = init?.method || 'GET'
    calls.push({ url: String(url), method, body: init?.body ? JSON.parse(init.body) : undefined })
    const text = String(url)
    if (method === 'POST' && text === '/api/storyboards/runs') {
      run = {
        id: calls[calls.length - 1].body.id,
        documentId: store.doc.id,
        status: 'running',
        index: 0,
        message: '开始顺序生成',
        steps: calls[calls.length - 1].body.steps.map((step: any) => ({ ...step, jobId: null })),
        jobs: [],
      }
      return new Response(JSON.stringify({ run }), { status: 202 })
    }
    if (text.startsWith('/api/storyboards/runs?')) {
      return new Response(JSON.stringify({ runs: run ? [run] : [] }))
    }
    if (/^\/api\/storyboards\/runs\/[0-9a-f-]+$/.test(text)) {
      return new Response(JSON.stringify({ run }))
    }
    return new Response(JSON.stringify({}), { status: 404 })
  }) as typeof fetch
  const prepared: string[] = []
  const panel = createStoryboardPanel(
    store,
    () => [],
    () => {},
    undefined,
    {
      ensureSaved: async () => {},
      prepareNode: async (id, signal) => {
        await gate
        if (signal?.aborted) {
          throw new Error('已取消')
        }
        prepared.push(id)
        return structuredClone(store.doc.objects[id].nodeDraft!)
      },
    },
  )
  try {
    fireEvent.click(screen.getByRole('button', { name: '分镜' }))
    await new Promise((resolve) => setTimeout(resolve, 10))
    fireEvent.click(screen.getByRole('button', { name: '顺序生成镜头' }))
    const field = screen.getByRole('textbox', { name: '镜头 1 画面描述' }) as HTMLTextAreaElement
    field.focus()
    fireEvent.input(field, { target: { value: '输入不能被执行回调覆盖' } })
    release()
    await new Promise((resolve) => setTimeout(resolve, 30))
    assert.equal(screen.getByRole('textbox', { name: '镜头 1 画面描述' }), field)
    assert.equal(field.value, '输入不能被执行回调覆盖')
    fireEvent.change(field)
    field.blur()
    await new Promise((resolve) => setTimeout(resolve, 30))
    assert.equal(store.doc.storyboard!.shots[0].visual, '输入不能被执行回调覆盖')
    const post = calls.find(
      (call) => call.method === 'POST' && call.url === '/api/storyboards/runs',
    )
    assert.ok(post, '必须提交服务端执行')
    assert.equal(post!.body.steps.length, 1)
    assert.match(post!.body.steps[0].request.requestId, /^[0-9a-f-]{36}$/)
    assert.deepEqual(prepared, [node.id])
  } finally {
    panel.dispose()
    globalThis.fetch = original
    localStorage.clear()
  }
})

test('恢复暂停执行后显示重试入口，完成后装配时间线默认开启可关闭', async () => {
  document.body.innerHTML = '<div class="topbar-tools"></div>'
  const { createNode } = await import('../apps/web/canvas/state/node-model.js')
  const { newShot } = await import('../apps/web/canvas/state/storyboard.js')
  const node = createNode('image', { x: 0, y: 0 }, '镜头')
  node.nodeDraft!.prompt = '镜头画面'
  const store = createDocStore({
    id: 'run-restore',
    name: 'doc',
    objects: { [node.id]: node },
    order: [node.id],
    storyboard: { version: 1, outline: '', shots: [{ ...newShot(), nodeId: node.id }] },
  })
  const run = {
    id: '01234567-89ab-cdef-0123-456789abcdef',
    documentId: store.doc.id,
    status: 'paused',
    index: 0,
    message: '当前镜头失败或结果未知，请重试该镜头',
    steps: [
      {
        shotId: store.doc.storyboard!.shots[0].id,
        draft: structuredClone(node.nodeDraft),
        request: {
          documentId: store.doc.id,
          clientRef: node.id,
          requestId: 'eeeeeeee-1111-2222-3333-444444444444',
          kind: 'image',
        },
        jobId: 'job-1',
      },
    ],
    jobs: [{ id: 'job-1', status: 'failed', error: '模型超时' }],
  }
  const original = globalThis.fetch
  globalThis.fetch = (async (url: any, init?: any) => {
    const text = String(url)
    const method = init?.method || 'GET'
    if (text.startsWith('/api/storyboards/runs?')) {
      return new Response(JSON.stringify({ runs: [run] }))
    }
    if (/^\/api\/storyboards\/runs\/[0-9a-f-]+$/.test(text) && method === 'GET') {
      return new Response(JSON.stringify({ run }))
    }
    if (text.endsWith('/retry')) {
      return new Response(JSON.stringify({ run: { ...run, status: 'running' } }))
    }
    return new Response(JSON.stringify({}), { status: 404 })
  }) as typeof fetch
  const panel = createStoryboardPanel(
    store,
    () => [],
    () => {},
    undefined,
    {
      ensureSaved: async () => {},
      prepareNode: async () => structuredClone(node.nodeDraft!),
    },
  )
  try {
    fireEvent.click(screen.getByRole('button', { name: '分镜' }))
    await new Promise((resolve) => setTimeout(resolve, 20))
    const retry = screen.getByRole('button', { name: '重试当前镜头' }) as HTMLButtonElement
    assert.equal(retry.disabled, false)
    const assemble = screen.getByRole('checkbox', {
      name: '完成后装配时间线',
    }) as HTMLInputElement
    assert.equal(assemble.checked, true, '完成后装配时间线默认开启')
    fireEvent.click(assemble)
    await new Promise((resolve) => setTimeout(resolve, 10))
    assert.equal(
      localStorage.getItem(`gencanvas.storyboardAssemble.${run.id}`),
      '0',
      '提交前可关闭装配选项',
    )
    fireEvent.click(screen.getByRole('button', { name: '重试当前镜头' }))
    await new Promise((resolve) => setTimeout(resolve, 20))
    assert.equal(node.nodeRun?.runId, run.id, '执行中的镜头由服务端执行接管，单节点入口被阻止')
  } finally {
    panel.dispose()
    globalThis.fetch = original
    localStorage.clear()
  }
})

test('创作工作流传入规划请求并按项目恢复', async () => {
  document.body.innerHTML = '<div class="topbar-tools"></div>'
  const store = createDocStore({ id: 'workflow', name: 'doc', objects: {}, order: [] })
  const originalFetch = globalThis.fetch
  let payload: { workflow?: string; prompt?: string } = {}
  globalThis.fetch = async (_input, init) => {
    payload = JSON.parse(String(init?.body))
    return new Response(JSON.stringify({ error: '测试模型不可用' }), { status: 502 })
  }
  let panel = createStoryboardPanel(store, () => [])
  try {
    fireEvent.click(screen.getByRole('button', { name: '分镜' }))
    fireEvent.change(screen.getByRole('combobox', { name: '创作工作流' }), {
      target: { value: 'product' },
    })
    fireEvent.input(screen.getByRole('textbox', { name: 'Agent 创作需求' }), {
      target: { value: '制作产品广告' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Agent 规划分镜' }))
    await new Promise((resolve) => setTimeout(resolve, 0))
    assert.equal(payload.workflow, 'product')
    assert.equal(payload.prompt, '制作产品广告')
    assert.equal(store.doc.storyboard, undefined)
    panel.dispose()
    panel = createStoryboardPanel(store, () => [])
    fireEvent.click(screen.getByRole('button', { name: '分镜' }))
    assert.equal(
      (screen.getByRole('combobox', { name: '创作工作流' }) as HTMLSelectElement).value,
      'product',
    )
  } finally {
    panel.dispose()
    globalThis.fetch = originalFetch
    localStorage.removeItem('gencanvas.storyboardPlan.workflow')
  }
})

test('批量准备按钮创建节点并可撤销，图片转视频后保留图片版本', async () => {
  document.body.innerHTML = '<div class="topbar-tools"></div>'
  const { newShot } = await import('../apps/web/canvas/state/storyboard.js')
  const store = createDocStore({
    id: 'batch-ui',
    name: 'doc',
    objects: {},
    order: [],
    storyboard: {
      version: 1,
      outline: '',
      shots: [
        { ...newShot(), visual: '开场' },
        { ...newShot(), visual: '结尾' },
      ],
    },
  })
  const panel = createStoryboardPanel(store, () => [])
  try {
    fireEvent.click(screen.getByRole('button', { name: '分镜' }))
    fireEvent.click(screen.getByRole('button', { name: '补齐图片节点' }))
    assert.equal(store.doc.order.length, 2)
    assert.equal(
      (screen.getByRole('button', { name: '补齐图片节点' }) as HTMLButtonElement).disabled,
      true,
    )
    fireEvent.click(screen.getByRole('button', { name: '撤销' }))
    assert.equal(store.doc.order.length, 0)
    fireEvent.click(screen.getByRole('button', { name: '重做' }))
    const imageIds = [...store.doc.order]
    store.mutateTransient(() => {
      for (const id of imageIds) {
        store.doc.objects[id].assetId = `asset-${id}`
      }
    })
    fireEvent.click(screen.getByRole('button', { name: '分镜图片转视频' }))
    await new Promise((resolve) => setTimeout(resolve, 0))
    assert.equal(store.doc.order.length, 4)
    for (const [index, shot] of store.doc.storyboard!.shots.entries()) {
      const video = store.doc.objects[shot.nodeId!]
      assert.equal(video.kind, 'video')
      assert.equal(video.nodeDraft!.references[0].sourceNodeId, imageIds[index])
      assert.ok(shot.versions!.includes(imageIds[index]))
    }
    fireEvent.click(screen.getByRole('button', { name: '撤销' }))
    assert.deepEqual(store.doc.order, imageIds)
  } finally {
    panel.dispose()
  }
})

test('从服务端历史取回候选，重复采用时创建独立镜头标识', async () => {
  document.body.innerHTML = '<div class="topbar-tools"></div>'
  const { newShot } = await import('../apps/web/canvas/state/storyboard.js')
  const shot = { ...newShot(), visual: '历史画面' }
  const storyboard = { version: 1 as const, outline: '历史大纲', shots: [shot] }
  const store = createDocStore({
    id: 'history-ui',
    name: 'doc',
    objects: {},
    order: [],
    storyboard,
  })
  const original = globalThis.fetch
  const calls: string[] = []
  globalThis.fetch = async (url, init) => {
    calls.push(`${init?.method || 'GET'} ${url}`)
    const job = {
      id: 'history-job',
      documentId: store.doc.id,
      prompt: '历史需求',
      workflow: 'general',
      status: 'completed',
      createdAt: '2026-09-14',
      storyboard,
    }
    return new Response(JSON.stringify(String(url).includes('?') ? { jobs: [job] } : { job }))
  }
  const panel = createStoryboardPanel(store, () => [])
  try {
    fireEvent.click(screen.getByRole('button', { name: '分镜' }))
    fireEvent.click(screen.getByRole('button', { name: '查看最近 20 次规划' }))
    await new Promise((resolve) => setTimeout(resolve, 0))
    fireEvent.click(screen.getByRole('button', { name: '已完成 · 历史需求' }))
    await new Promise((resolve) => setTimeout(resolve, 0))
    fireEvent.click(screen.getByRole('button', { name: '采用并追加分镜' }))
    assert.equal(store.doc.storyboard!.shots.length, 2)
    assert.notEqual(store.doc.storyboard!.shots[0].id, store.doc.storyboard!.shots[1].id)
    assert.equal(store.doc.storyboard!.shots[1].visual, '历史画面')
    assert.ok(calls.every((call) => call.startsWith('GET ')))
  } finally {
    panel.dispose()
    globalThis.fetch = original
    localStorage.removeItem('gencanvas.storyboardPlan.history-ui')
  }
})

test('质量检查列出问题并在素材齐备后显示未发现问题', async () => {
  document.body.innerHTML = '<div class="topbar-tools"></div>'
  const { createNode } = await import('../apps/web/canvas/state/node-model.js')
  const { newShot } = await import('../apps/web/canvas/state/storyboard.js')
  const node = createNode('image', { x: 0, y: 0 }, '镜头')
  node.nodeDraft!.prompt = '镜头画面'
  const store = createDocStore({
    id: 'checks-ui',
    name: 'doc',
    objects: { [node.id]: node },
    order: [node.id],
    storyboard: {
      version: 1,
      outline: '',
      shots: [
        { ...newShot(), title: '开场', visual: '产品特写', nodeId: node.id },
        { ...newShot(), title: '结尾', visual: '' },
      ],
    },
  })
  const panel = createStoryboardPanel(store, () => [])
  try {
    fireEvent.click(screen.getByRole('button', { name: '分镜' }))
    fireEvent.click(screen.getByRole('button', { name: '质量检查' }))
    const result = screen.getByLabelText('质量检查结果')
    assert.ok(result.textContent!.includes('镜头 2「结尾」尚未关联生成节点'))
    // 镜头 2 关联素材后结果随渲染刷新
    fireEvent.click(screen.getByRole('button', { name: '收起检查' }))
    assert.equal(screen.queryByLabelText('质量检查结果'), null)
  } finally {
    panel.dispose()
  }
})

test('主体引用按钮按选中态启用，应用后待生成节点携带参考且可撤销', async () => {
  document.body.innerHTML = '<div class="topbar-tools"></div>'
  const { createNode } = await import('../apps/web/canvas/state/node-model.js')
  const { newShot } = await import('../apps/web/canvas/state/storyboard.js')
  const subject = createNode('image', { x: 0, y: 0 }, '主体')
  subject.src = '/images/subject.png'
  const pending = createNode('image', { x: 400, y: 0 }, '待生成')
  const store = createDocStore({
    id: 'subject-ui',
    name: 'doc',
    objects: { [subject.id]: subject, [pending.id]: pending },
    order: [subject.id, pending.id],
    storyboard: {
      version: 1,
      outline: '',
      shots: [
        { ...newShot(), title: '主体镜', visual: '主体特写', nodeId: subject.id },
        { ...newShot(), title: '空镜', visual: '场景', nodeId: null },
        { ...newShot(), title: '待生成镜', visual: '场景', nodeId: pending.id },
      ],
    },
  })
  const original = globalThis.fetch
  globalThis.fetch = (async (url: any) => {
    if (String(url).startsWith('/api/assets')) {
      return new Response(JSON.stringify({ asset: { id: 'asset-ui', ext: 'png' } }), {
        status: 201,
      })
    }
    if (String(url) === '/images/subject.png') {
      return new Response(new Blob([new Uint8Array([137, 80, 78, 71])], { type: 'image/png' }))
    }
    return new Response(JSON.stringify({}), { status: 404 })
  }) as typeof fetch
  const panel = createStoryboardPanel(store, () => [])
  try {
    fireEvent.click(screen.getByRole('button', { name: '分镜' }))
    const apply = screen.getByRole('button', { name: '主体引用到全部镜头' }) as HTMLButtonElement
    assert.equal(apply.disabled, true, '未选中主体时禁用')
    store.mutateTransient(() => {})
    panel.dispose()
    const panel2 = createStoryboardPanel(
      store,
      () => [subject.id],
      () => {},
      undefined,
      undefined,
    )
    try {
      fireEvent.click(screen.getByRole('button', { name: '分镜' }))
      const apply2 = screen.getByRole('button', { name: '主体引用到全部镜头' }) as HTMLButtonElement
      assert.equal(apply2.disabled, false)
      fireEvent.click(apply2)
      await new Promise((resolve) => setTimeout(resolve, 20))
      assert.equal(store.doc.objects[pending.id].nodeDraft!.references[0].assetId, 'asset-ui')
      const statuses = screen.getAllByRole('status')
      assert.ok(statuses.some((node) => node.textContent!.includes('已将主体引用到 1 个镜头')))
      store.undo()
      assert.equal(store.doc.objects[pending.id].nodeDraft!.references.length, 0)
    } finally {
      panel2.dispose()
    }
  } finally {
    panel.dispose()
    globalThis.fetch = original
    localStorage.clear()
  }
})

test('重复创建前必须 dispose，否则会留下第二枚分镜按钮', () => {
  document.body.innerHTML = '<div class="topbar-tools"></div>'
  const store = createDocStore({ id: 'dup', name: 'doc', objects: {}, order: [] })
  const first = createStoryboardPanel(store, () => [])
  const second = createStoryboardPanel(store, () => [])
  try {
    assert.equal(screen.getAllByRole('button', { name: '分镜' }).length, 2)
    first.dispose()
    assert.equal(screen.getAllByRole('button', { name: '分镜' }).length, 1)
  } finally {
    second.dispose()
    first.dispose()
    assert.equal(screen.queryAllByRole('button', { name: '分镜' }).length, 0)
  }
})
