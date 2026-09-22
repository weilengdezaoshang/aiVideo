import test from 'node:test'
import assert from 'node:assert/strict'
import {
  jobEventToTransition,
  applyTransition,
  reconcile,
  imageRecordToAsset,
} from '../apps/web/canvas/state/generation-reducer.js'
import { connectGenerationEvents } from '../apps/web/canvas/state/generation-events.js'

/* eslint-disable @typescript-eslint/no-explicit-any -- 测试聚焦 reducer 语义,文档结构刻意宽松 */
import type { CanvasDocData } from '../apps/web/canvas/state/doc-store.js'

const anyDoc = (objects: Record<string, any>): CanvasDocData => ({
  id: 'doc-test',
  name: '测试文档',
  objects,
  order: Object.keys(objects),
  revision: 0,
})

const placeholder = (id: string, jobId: string, x = 0, y = 0) => ({
  id,
  kind: 'placeholder',
  x,
  y,
  width: 512,
  height: 512,
  gen: { jobId },
})

/* 六场景(PRD F2 验收):提交 / 进度 / 落位 / 取消 / 失败重试 / 刷新恢复 */

test('场景① 提交:SSE 先于 POST 响应到达,clientRef 对齐占位对象', () => {
  const doc = anyDoc({})
  // 占位对象已由本地命令创建,但 gen.jobId 尚未回填(POST 未返回)——
  // 真实流程:先本地建占位(clientRef),事件携带 clientRef 直接命中
  doc.objects.p1 = { id: 'p1', kind: 'placeholder', x: 0, y: 0, width: 512, height: 512, gen: {} }
  const changed = applyTransition(
    doc,
    'p1',
    jobEventToTransition(doc.objects.p1, {
      id: 'job-1',
      status: 'queued',
      progress: 0,
      message: '排队中',
    }),
    undefined,
  )
  assert.equal(changed, true)
  assert.equal(doc.objects.p1.gen!.progress, 0)
  // 关键:gen.jobId 为空时必须用事件自带的 job.id 回填,否则对账/取消都找不到该占位
  assert.equal(doc.objects.p1.gen!.jobId, 'job-1')
})

test('场景② 进度:running 事件推进百分比与文案', () => {
  const doc = anyDoc({ p1: placeholder('p1', 'job-1') })
  applyTransition(
    doc,
    'p1',
    jobEventToTransition(doc.objects.p1, {
      id: 'job-1',
      status: 'running',
      progress: 0.42,
      message: '生成中',
    }),
  )
  assert.equal(doc.objects.p1.gen!.progress, 0.42)
  assert.equal(doc.objects.p1.gen!.message, '生成中')
})

test('场景③ 落位:completed → 图片对象(带谱系字段与资源引用)', () => {
  const doc = anyDoc({ p1: placeholder('p1', 'job-1') })
  const imageRecord = {
    id: 'img-9',
    url: '/images/a.png',
    file: 'a.png',
    params: { kind: 'image', width: 1024, height: 768 },
  }
  applyTransition(
    doc,
    'p1',
    jobEventToTransition(doc.objects.p1, {
      id: 'job-1',
      status: 'completed',
      images: [imageRecord],
    }),
    imageRecordToAsset,
  )
  const obj = doc.objects.p1
  assert.equal(obj.kind, 'image')
  assert.equal(obj.src, '/images/a.png')
  assert.equal(obj.width, 1024)
  assert.equal(obj.height, 768)
  assert.equal(obj.gen, undefined)
})

test('场景④ 取消:服务端取消= failed 事件 → 错误卡(可重试)', () => {
  const doc = anyDoc({ p1: placeholder('p1', 'job-1') })
  applyTransition(
    doc,
    'p1',
    jobEventToTransition(doc.objects.p1, {
      id: 'job-1',
      status: 'failed',
      error: '已取消',
    }),
  )
  assert.equal(doc.objects.p1.kind, 'error')
  assert.equal(doc.objects.p1.errorDetail, '已取消')
})

test('场景⑤ 失败重试的参数来源:谱系 params 保留在 lineage(协议约定)', () => {
  // 重试 = 按 lineage.params 原样重发,由 flows 层(T10)读取;此处锁定 lineage 字段不被 reducer 破坏
  const doc = anyDoc({
    p1: { ...placeholder('p1', 'job-1'), lineage: { fromId: 'src-1', params: { prompt: 'x' } } },
  })
  applyTransition(
    doc,
    'p1',
    jobEventToTransition(doc.objects.p1, {
      id: 'job-1',
      status: 'failed',
      error: '后端超时',
    }),
  )
  assert.deepEqual(doc.objects.p1.lineage, { fromId: 'src-1', params: { prompt: 'x' } })
})

test('场景⑥ 刷新恢复:对账把占位校正为 服务端状态/丢失', () => {
  const doc = anyDoc({
    running: placeholder('running', 'job-run'),
    done: placeholder('done', 'job-done'),
    lost: placeholder('lost', 'job-gone'),
    normal: { id: 'normal', kind: 'image', x: 9, y: 9, width: 8, height: 8 },
  })
  const serverJobs = [
    { id: 'job-run', status: 'running', progress: 0.6, message: '生成中' },
    {
      id: 'job-done',
      status: 'completed',
      images: [{ id: 'i', url: '/images/b.png', file: 'b.png', params: { kind: 'image' } }],
    },
  ]
  const { changedIds, lostIds } = reconcile(doc, serverJobs, imageRecordToAsset)

  assert.equal(doc.objects.running.kind, 'placeholder')
  assert.equal(doc.objects.running.gen!.progress, 0.6)
  assert.equal(doc.objects.done.kind, 'image')
  assert.equal(doc.objects.done.src, '/images/b.png')
  assert.equal(doc.objects.lost.kind, 'error')
  assert.match(doc.objects.lost.errorDetail!, /任务丢失/)
  assert.equal(doc.objects.normal.kind, 'image', '非占位对象不动')
  assert.ok(changedIds.includes('lost') && lostIds.includes('lost'))
})

test('events 层:未知 jobId 丢弃、clientRef 对齐、重连触发对账', async () => {
  // 模拟 EventSource
  const listeners = new Map<string, (e: { data: string }) => void>()
  const es = {
    addEventListener(type: string, fn: (e: { data: string }) => void) {
      listeners.set(type, fn)
    },
    close() {},
    readyState: 1,
  }
  /** 模拟文档(带 mutateTransient 语义) */
  const doc = anyDoc({
    p1: placeholder('p1', 'job-1'),
    ghost: placeholder('ghost', 'job-x', 50, 50),
  })
  const docStore = {
    doc,
    mutateTransient(fn: (d: typeof doc) => unknown) {
      return fn(doc)
    },
  }
  const connectionChanges: boolean[] = []
  const reconciledRef: { value: { changed: number; lost: number } | null } = { value: null }
  const fetchCalls: string[] = []
  const fetchImpl = async (url: string) => {
    fetchCalls.push(url)
    return {
      json: async () => ({
        jobs: [
          {
            id: 'job-1',
            status: 'completed',
            images: [{ id: 'i1', url: '/images/z.png', file: 'z.png', params: { kind: 'image' } }],
          },
        ],
      }),
    }
  }
  const client = connectGenerationEvents({
    docStore,
    onConnectionChange: (c) => connectionChanges.push(c),
    onReconciled: (r) => {
      reconciledRef.value = r
    },
    eventSourceFactory: () => es,
    fetchImpl: fetchImpl as unknown as typeof fetch,
  })

  // 未知 jobId 的 job 事件:ghost 的 jobId 是 job-x,事件是 job-other → 丢弃
  listeners.get('job')!({
    data: JSON.stringify({ id: 'job-other', status: 'running', progress: 1 }),
  })
  assert.equal(doc.objects.ghost.kind, 'placeholder')

  // open → 连接回调 + 触发对账(snapshot 亦可)
  listeners.get('open')!({ data: '' })
  await new Promise((r) => setTimeout(r, 20))
  assert.deepEqual(connectionChanges, [true])
  assert.ok(reconciledRef.value)
  assert.equal(reconciledRef.value.changed, 2, 'job-1 落位 + ghost 丢失,共两处变更')
  assert.equal(doc.objects.p1.kind, 'image')
  // ghost 的 job-x 不在服务端列表 → 任务丢失
  assert.equal(doc.objects.ghost.kind, 'error')
  assert.equal(reconciledRef.value.lost, 1)
  assert.ok(fetchCalls.includes('/api/jobs?all=1&limit=100'))
  client.close()
})
