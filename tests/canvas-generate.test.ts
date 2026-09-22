import test from 'node:test'
import assert from 'node:assert/strict'
import {
  findFreePosition,
  overlapsAny,
  variantRowAnchors,
  clampIntoRect,
} from '../apps/web/canvas/state/placement.js'
import { submitGeneration, cancelPlaceholder } from '../apps/web/canvas/flows/generate.js'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'
import { cmdAddObjects } from '../apps/web/canvas/state/commands.js'
import { selectCanvasObject } from '../apps/web/canvas/state/generation-entity.js'

/* eslint-disable @typescript-eslint/no-explicit-any -- 测试对生成参数与文档结构使用宽类型 */

/* ---------- 螺旋落位纯函数 ---------- */

test('锚点空时直接落位', () => {
  const doc = { objects: {} }
  const pos = findFreePosition(doc, { x: 100, y: 100 }, { width: 256, height: 256 })
  assert.deepEqual(pos, { x: 100, y: 100 })
})

test('锚点被占:顺时针螺旋找到第一个空位(右侧优先)', () => {
  const doc = {
    objects: { a: { x: 100, y: 100, width: 256, height: 256 } },
  }
  const pos = findFreePosition(doc, { x: 100, y: 100 }, { width: 256, height: 256 }, { gap: 40 })
  // 环 1 顶边起点 = 右上 (100+296, 100-296) → 空,胜出
  assert.deepEqual(pos, { x: 396, y: -196 })
})

test('连续以图生图形成右侧变体群(空间谱系布局)', () => {
  const objects: Record<string, { x: number; y: number; width: number; height: number }> = {}
  // 源图 a 在 (0,0);两次以它为锚的落位应排开不重叠
  objects.a = { x: 0, y: 0, width: 256, height: 256 }
  const p1 = findFreePosition(
    { objects },
    { x: 296, y: 0 },
    { width: 256, height: 256 },
    { gap: 40 },
  )
  objects.b = { x: p1.x, y: p1.y, width: 256, height: 256 }
  const p2 = findFreePosition(
    { objects },
    { x: 296, y: 0 },
    { width: 256, height: 256 },
    { gap: 40 },
  )
  objects.c = { x: p2.x, y: p2.y, width: 256, height: 256 }

  const ids = ['a', 'b', 'c']
  for (let i = 0; i < ids.length; i++) {
    for (let j = i + 1; j < ids.length; j++) {
      const A = objects[ids[i]] as any
      const B = objects[ids[j]] as any
      const sep =
        A.x + A.width < B.x || B.x + B.width < A.x || A.y + A.height < B.y || B.y + B.height < A.y
      assert.ok(sep, `${ids[i]} 与 ${ids[j]} 不应重叠`)
    }
  }
})

test('overlapsAny 保留 GAP 间距', () => {
  const boxes = { a: { x: 0, y: 0, width: 100, height: 100 } }
  // 间距不足 40:视为重叠
  assert.equal(overlapsAny(boxes, { x: 120, y: 0, width: 100, height: 100 }, 40), true)
  // 恰好留出 40:不重叠
  assert.equal(overlapsAny(boxes, { x: 140, y: 0, width: 100, height: 100 }, 40), false)
})

test('生成四个变体时占位锚点在源图右侧同一行', () => {
  const anchors = variantRowAnchors({ x: 100, y: 80, width: 256, height: 192 }, 4, 40)
  assert.deepEqual(anchors, [
    { x: 396, y: 80 },
    { x: 692, y: 80 },
    { x: 988, y: 80 },
    { x: 1284, y: 80 },
  ])
})

/* ---------- 视野钳制(新建对象必须可见) ---------- */

test('视野内的候选位原样保留', () => {
  const view = { x: 0, y: 0, width: 1440, height: 900 }
  const pos = clampIntoRect({ x: 100, y: 100 }, { width: 320, height: 320 }, view)
  assert.deepEqual(pos, { x: 100, y: 100 })
})

test('螺旋走出视野上方(y 为负)时钳回视野内', () => {
  const view = { x: 0, y: 0, width: 1440, height: 900 }
  const pos = clampIntoRect({ x: 396, y: -196 }, { width: 320, height: 320 }, view)
  assert.equal(pos.y, 24) // margin
  assert.equal(pos.x, 396) // x 本就在视野内,不动
})

test('视野右/下越界钳到右/下边界内侧', () => {
  const view = { x: 0, y: 0, width: 800, height: 600 }
  const pos = clampIntoRect({ x: 760, y: 560 }, { width: 320, height: 320 }, view)
  assert.equal(pos.x, 800 - 24 - 320)
  assert.equal(pos.y, 600 - 24 - 320)
})

test('视野小于对象时落到左上角 margin 处(保证左上角可见)', () => {
  const view = { x: 0, y: 0, width: 200, height: 200 }
  const pos = clampIntoRect({ x: 5000, y: 5000 }, { width: 1024, height: 1024 }, view)
  assert.deepEqual(pos, { x: 24, y: 24 })
})

/* ---------- submitGeneration(依赖注入 fetch) ---------- */

interface FetchCall {
  url: string
  init: RequestInit
}
function fakeFetch(responses: Array<{ status: number; body: any }>) {
  const calls: FetchCall[] = []
  const impl = (async (url: string, init: RequestInit = {}) => {
    calls.push({ url, init })
    const next = responses.shift()
    if (!next) {
      throw new Error('no more responses')
    }
    return new Response(JSON.stringify(next.body), { status: next.status })
  }) as unknown as typeof fetch
  return { impl, calls }
}

test('提交成功:占位入 undo 栈 + jobId 回填 + batchCount 强制 1', async () => {
  const store = createDocStore({ id: 'd', name: 't', objects: {}, order: [] })
  const { impl, calls } = fakeFetch([{ status: 202, body: { jobId: 'job-77' } }])
  let createdId = ''

  const result = await submitGeneration({
    docStore: store,
    genParams: { prompt: '猫', width: 512, height: 512, batchCount: 4 },
    anchor: { x: 0, y: 0 },
    onPlaceholderCreated: (placeholder: { id: string }) => {
      createdId = placeholder.id
    },
    fetchImpl: impl,
  })
  assert.equal(result.ok, true)
  assert.equal(result.jobId, 'job-77')
  const objId = result.objId!
  assert.equal(createdId, objId, '提交请求前即可定位新占位卡')
  assert.equal(store.doc.objects[objId].kind, 'placeholder')
  assert.equal(store.doc.generations?.[result.generationId!]?.jobId, 'job-77')

  const sent = JSON.parse(String(calls[0].init.body))
  assert.equal(sent.batchCount, 1, '画布提交强制单张')
  assert.equal(sent.clientRef, objId)

  // 撤销提交 → 占位消失(占位创建与回填合并为一条命令的效果)
  assert.equal(store.undo(), true)
  assert.equal(store.doc.objects[objId], undefined)
})

test('提交失败(4xx):占位转错误卡并携带原因', async () => {
  const store = createDocStore({ id: 'd', name: 't', objects: {}, order: [] })
  const { impl } = fakeFetch([{ status: 400, body: { error: '提示词不能为空' } }])
  const result = await submitGeneration({
    docStore: store,
    genParams: { prompt: '', width: 512, height: 512 },
    fetchImpl: impl,
  })
  assert.equal(result.ok, false)
  const obj = selectCanvasObject(store.doc, result.objId!)!
  assert.equal(obj.kind, 'error')
  assert.equal(obj.errorDetail, '提示词不能为空')
})

test('网络异常:占位转错误卡', async () => {
  const store = createDocStore({ id: 'd', name: 't', objects: {}, order: [] })
  const impl = (async () => {
    throw new Error('fetch failed')
  }) as unknown as typeof fetch
  const result = await submitGeneration({
    docStore: store,
    genParams: { prompt: 'x', width: 512, height: 512 },
    fetchImpl: impl,
  })
  assert.equal(result.ok, false)
  assert.equal(selectCanvasObject(store.doc, result.objId!)?.kind, 'error')
})

test('取消占位:DELETE 任务 + 占位移除(可撤销)', async () => {
  const store = createDocStore({ id: 'd', name: 't', objects: {}, order: [] })
  const calls: FetchCall[] = []
  const impl = (async (url: string, init: RequestInit = {}) => {
    calls.push({ url, init })
    return new Response('{}', { status: 200 })
  }) as unknown as typeof fetch

  store.apply(
    cmdAddObjects([
      { id: 'p1', kind: 'placeholder', x: 0, y: 0, width: 10, height: 10, gen: { jobId: 'j1' } },
    ]),
  )
  await cancelPlaceholder({ docStore: store, objId: 'p1', fetchImpl: impl })
  assert.equal(calls[0].url, '/api/jobs/j1')
  assert.equal(calls[0].init.method, 'DELETE')
  assert.equal(store.doc.objects.p1, undefined)
  assert.equal(store.undo(), true)
  assert.equal((store.doc.objects.p1 as any)?.kind, 'placeholder')
})

/* ---------- retryError(T10) ---------- */

test('重试:错误卡原位转占位,按谱系参数重发并回填 jobId', async () => {
  const store = createDocStore({ id: 'd', name: 't', objects: {}, order: [] })
  store.apply(
    cmdAddObjects([
      {
        id: 'err-1',
        kind: 'error',
        x: 50,
        y: 60,
        width: 300,
        height: 150,
        errorDetail: '后端超时',
        lineage: { fromId: 'src-1', params: { prompt: '猫', width: 512, height: 512, seed: 7 } },
      },
    ]),
  )
  const { impl, calls } = fakeFetch([{ status: 202, body: { jobId: 'job-retry' } }])

  const result = await import('../apps/web/canvas/flows/generate.js').then((m) =>
    m.retryError({
      docStore: store,
      objId: 'err-1',
      resolveReference: async () => null, // 源图不在:退化纯文生图
      fetchImpl: impl,
    }),
  )
  assert.equal(result.ok, true)
  assert.equal((result as any).jobId, 'job-retry')
  const obj = selectCanvasObject(store.doc, 'err-1') as any
  assert.equal(obj.kind, 'placeholder')
  assert.equal(obj.gen?.jobId, 'job-retry')
  assert.equal(obj.x, 50, '原位转占位')

  const sent = JSON.parse(String(calls[0].init.body))
  assert.equal(sent.prompt, '猫', '谱系参数原样重发')
  assert.equal(sent.seed, 7)
  assert.equal(sent.initImage, undefined, '参考图不可得时退化为文生图')
})

test('重试携带参考图:源图可得时 initImage 透传', async () => {
  const store = createDocStore({ id: 'd', name: 't', objects: {}, order: [] })
  store.apply(
    cmdAddObjects([
      {
        id: 'err-2',
        kind: 'error',
        x: 0,
        y: 0,
        width: 300,
        height: 150,
        lineage: { fromId: 'src-9', params: { prompt: '夜景', width: 512, height: 512 } },
      },
    ]),
  )
  const { impl, calls } = fakeFetch([{ status: 202, body: { jobId: 'job-2' } }])
  const result = await import('../apps/web/canvas/flows/generate.js').then((m) =>
    m.retryError({
      docStore: store,
      objId: 'err-2',
      resolveReference: async () => ({ dataUrl: 'data:image/png;base64,AAA' }),
      fetchImpl: impl,
    }),
  )
  assert.equal(result.ok, true)
  const sent = JSON.parse(String(calls[0].init.body))
  assert.equal(sent.initImage, 'data:image/png;base64,AAA')
})

test('重试再次失败:回到错误卡并更新原因', async () => {
  const store = createDocStore({ id: 'd', name: 't', objects: {}, order: [] })
  store.apply(
    cmdAddObjects([
      {
        id: 'err-3',
        kind: 'error',
        x: 0,
        y: 0,
        width: 300,
        height: 150,
        lineage: { fromId: 'src', params: { prompt: 'x', width: 512, height: 512 } },
      },
    ]),
  )
  const { impl } = fakeFetch([{ status: 500, body: { error: '后端超时' } }])
  await import('../apps/web/canvas/flows/generate.js').then((m) =>
    m.retryError({ docStore: store, objId: 'err-3', fetchImpl: impl }),
  )
  const obj = selectCanvasObject(store.doc, 'err-3') as any
  assert.equal(obj.kind, 'error')
  assert.equal(obj.errorDetail, '后端超时')
})

test('重试非错误卡/缺谱系:拒绝', async () => {
  const store = createDocStore({ id: 'd', name: 't', objects: {}, order: [] })
  store.apply(
    cmdAddObjects([
      {
        id: 'img-1',
        kind: 'image',
        x: 0,
        y: 0,
        width: 10,
        height: 10,
      },
    ]),
  )
  const { impl } = fakeFetch([])
  const r1 = await import('../apps/web/canvas/flows/generate.js').then((m) =>
    m.retryError({ docStore: store, objId: 'img-1', fetchImpl: impl }),
  )
  assert.equal(r1.ok, false)
  store.apply(cmdAddObjects([{ id: 'err-x', kind: 'error', x: 0, y: 0, width: 10, height: 10 }]))
  const r2 = await import('../apps/web/canvas/flows/generate.js').then((m) =>
    m.retryError({ docStore: store, objId: 'err-x', fetchImpl: impl }),
  )
  assert.equal(r2.ok, false)
})
