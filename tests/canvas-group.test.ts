import test from 'node:test'
import assert from 'node:assert/strict'
import { reconcile, type ResolveImage } from '../apps/web/canvas/state/generation-reducer.js'
import {
  applyGroupJobs,
  groupCardState,
  groupJobs,
  groupTransitions,
  groupUnifiedMessage,
  isGroupTerminal,
  sweepLostGroupSlots,
} from '../apps/web/canvas/state/group-state.js'
import { groupInsertion } from '../apps/web/canvas/state/placement.js'

interface JobStub {
  id: string
  status: string
  groupId: string
  slot: number
  clientRef: string
  directionTitle: string
  images?: Array<{ url: string }>
  error?: string
  [key: string]: unknown
}

interface SlotObj {
  id: string
  kind: string
  groupId?: string
  gen?: Record<string, unknown>
  src?: string
  errorDetail?: string
}

function job(over: Partial<JobStub>): JobStub {
  const slot = over.slot ?? 0
  return {
    id: 'job-x',
    status: 'running',
    groupId: 'g1',
    slot,
    clientRef: `obj-${slot}`,
    directionTitle: '方向',
    ...over,
  }
}

const resolve: ResolveImage = (image) => ({
  kind: 'image',
  assetId: null,
  src: image.url ?? '',
  ext: 'png',
  width: 100,
  height: 100,
  hasThumbs: false,
})

test('groupJobs 按 groupId 分桶并按 slot 排序', () => {
  const map = groupJobs([job({ slot: 2 }), job({ slot: 0 }), job({ groupId: 'g2', slot: 9 })])
  assert.equal(map.size, 2)
  assert.deepEqual([...(map.get('g1')?.map((/** @type {any} */ j) => j.slot) ?? [])], [0, 2])
})

test('组未终结时四格统一状态,不逐张发布', () => {
  const slots = [
    job({ slot: 0, status: 'completed', images: [{ url: '/a.png' }] }),
    job({ slot: 1, status: 'running' }),
    job({ slot: 2, status: 'queued' }),
    job({ slot: 3, status: 'queued' }),
  ]
  assert.equal(isGroupTerminal(slots), false)
  assert.equal(groupUnifiedMessage(slots), '生成中…')
  const doc = {
    objects: Object.fromEntries(
      [0, 1, 2, 3].map((i) => [
        `obj-${i}`,
        {
          id: `obj-${i}`,
          kind: 'placeholder' as const,
          x: 0,
          y: 0,
          width: 320,
          height: 320,
          groupId: 'g1',
          gen: {},
        },
      ]),
    ),
  }
  const result = groupTransitions(doc, slots, 'g1')
  assert.equal(result.publishes, false)
  assert.equal(result.transitions.length, 4)
  for (const t of result.transitions) {
    assert.equal(t.transition.kind, 'progress')
    assert.equal(t.transition.message, '生成中…')
    assert.equal(t.transition.progress, undefined)
  }
})

test('组全部终结时整组发布:完成槽落位、失败槽转错误卡', () => {
  const slots = [
    job({ slot: 0, status: 'completed', images: [{ url: '/a.png' }] }),
    job({ slot: 1, status: 'failed', error: '上游超时' }),
    job({ slot: 2, status: 'completed', images: [{ url: '/c.png' }] }),
    job({ slot: 3, status: 'completed', images: [{ url: '/d.png' }] }),
  ]
  const doc = {
    objects: Object.fromEntries(
      [0, 1, 2, 3].map((i) => [
        `obj-${i}`,
        {
          id: `obj-${i}`,
          kind: 'placeholder' as const,
          x: 0,
          y: 0,
          width: 320,
          height: 320,
          groupId: 'g1',
          gen: {},
        },
      ]),
    ),
  }
  const result = groupTransitions(doc, slots, 'g1')
  assert.equal(result.publishes, true)
  assert.equal(result.transitions[0].transition.kind, 'complete')
  assert.equal(result.transitions[1].transition.kind, 'fail')
  assert.equal(result.transitions[1].transition.error, '上游超时')

  const applied = applyGroupJobs(doc, slots, resolve)
  assert.equal(applied.publishedGroups.includes('g1'), true)
  const o0 = doc.objects['obj-0'] as SlotObj
  const o1 = doc.objects['obj-1'] as SlotObj
  const o2 = doc.objects['obj-2'] as SlotObj
  assert.equal(o0.kind, 'image')
  assert.equal(o0.src, '/a.png')
  assert.equal(o1.kind, 'error')
  assert.equal(o1.errorDetail, '上游超时')
  assert.equal(o2.kind, 'image')
})

test('被删除的槽位不复活;组卡状态分级正确', () => {
  const slots = [
    job({ slot: 0, status: 'completed', images: [{ url: '/a.png' }], clientRef: 'gone' }),
    job({ slot: 1, status: 'failed', error: 'x' }),
  ]
  const doc = {
    objects: {
      'obj-1': {
        id: 'obj-1',
        kind: 'placeholder' as const,
        x: 0,
        y: 0,
        width: 320,
        height: 320,
        groupId: 'g1',
        gen: {},
      },
    },
  }
  const result = groupTransitions(doc, slots, 'g1')
  // 'gone' 对象不在文档里 → 只处理现存槽位
  assert.equal(result.transitions.length, 1)
  assert.equal(result.transitions[0].objId, 'obj-1')

  assert.equal(groupCardState(slots).key, 'partial')
  assert.equal(
    groupCardState([
      job({ status: 'completed', images: [{ url: '/x.png' }] }),
      job({ status: 'completed', images: [{ url: '/y.png' }] }),
    ]).key,
    'done',
  )
  assert.equal(groupCardState([job({ status: 'failed' })]).key, 'failed')
})

test('groupInsertion:派生结果插入源图右侧,同组右侧成员顺延,组外不动', () => {
  const doc = {
    objects: {
      src: { id: 'src', x: 0, y: 0, width: 100, height: 100, groupId: 'g1' },
      right: { id: 'right', x: 140, y: 0, width: 100, height: 100, groupId: 'g1' },
      far: { id: 'far', x: 280, y: 0, width: 100, height: 100, groupId: 'g1' },
      other: { id: 'other', x: 150, y: 0, width: 100, height: 100, groupId: 'g2' },
      below: { id: 'below', x: 10, y: 200, width: 100, height: 100, groupId: 'g1' },
    },
  }
  const insertion = groupInsertion(doc, 'src', { width: 80, height: 80 })
  // 源图右邻,间距 24
  assert.deepEqual({ x: insertion.x, y: insertion.y }, { x: 124, y: 0 })
  const moved = Object.fromEntries(insertion.moves.map((m) => [m.id, m.to.x]))
  // 同组右侧成员顺延(80 + 24)
  assert.equal(moved['right'], 244)
  assert.equal(moved['far'], 384)
  assert.equal(moved['other'], undefined)
  assert.equal(moved['below'], undefined)

  // 独立图片:直接右邻,不动任何人
  const solo = groupInsertion(
    { objects: { s: { id: 's', x: 0, y: 0, width: 50, height: 50 } } },
    's',
    {
      width: 50,
      height: 50,
    },
  )
  assert.equal(solo.x, 74)
  assert.equal(solo.moves.length, 0)
})

test('reconcile 不碰组槽位(单任务查无即丢的规则不适用于组)', () => {
  const doc = {
    objects: {
      g: {
        id: 'g',
        kind: 'placeholder' as const,
        x: 0,
        y: 0,
        width: 10,
        height: 10,
        groupId: 'grp-1',
        gen: { jobId: 'j-group' },
      },
      s: {
        id: 's',
        kind: 'placeholder' as const,
        x: 0,
        y: 0,
        width: 10,
        height: 10,
        gen: { jobId: 'j-single' },
      },
    } as Record<string, ReconcileObj>,
  }
  const result = reconcile(doc, [], undefined)
  // 单任务占位被标记丢失;组槽位保持占位状态等待整组逻辑处理
  assert.equal(doc.objects.s.kind, 'error')
  assert.equal(doc.objects.g.kind, 'placeholder')
  assert.deepEqual(result.lostIds, ['s'])
})

test('sweepLostGroupSlots:任务记录过期的组槽位转错误卡,在列的不动', () => {
  const jobs = [{ id: 'alive', status: 'running', groupId: 'g1' }]
  const doc = {
    objects: {
      aliveSlot: {
        id: 'aliveSlot',
        kind: 'placeholder' as const,
        x: 0,
        y: 0,
        width: 10,
        height: 10,
        groupId: 'g1',
        gen: { jobId: 'alive' },
      },
      staleSlot: {
        id: 'staleSlot',
        kind: 'placeholder' as const,
        x: 0,
        y: 0,
        width: 10,
        height: 10,
        groupId: 'g1',
        gen: { jobId: 'pruned' },
      },
      single: {
        id: 'single',
        kind: 'placeholder' as const,
        x: 0,
        y: 0,
        width: 10,
        height: 10,
        gen: { jobId: 'pruned-too' },
      },
    } as Record<string, ReconcileObj>,
  }
  const result = sweepLostGroupSlots(doc, jobs as JobStub[])
  const alive = doc.objects.aliveSlot
  const stale = doc.objects.staleSlot
  const single = doc.objects.single
  assert.equal(alive.kind, 'placeholder')
  assert.equal(stale.kind, 'error')
  assert.ok((stale.errorDetail ?? '').includes('过期'))
  // 单任务槽位不归 sweep 管(由 reconcile 兜底)
  assert.equal(single.kind, 'placeholder')
  assert.deepEqual(result.changedObjIds, ['staleSlot'])
})

interface ReconcileObj {
  id: string
  kind: 'image' | 'video' | 'placeholder' | 'error' | 'board' | 'text'
  x: number
  y: number
  width: number
  height: number
  groupId?: string
  gen?: { jobId: string; message?: string }
  errorDetail?: string
}

import { markDisconnected } from '../apps/web/canvas/state/generation-reducer.js'
import { applyCommand } from '../apps/web/canvas/state/commands.js'
import { confirmPendingGroup } from '../apps/web/canvas/flows/group-generate.js'

test('markDisconnected:绑定任务的占位统一切换断线文案,未绑定的不动', () => {
  const doc = {
    objects: {
      a: {
        id: 'a',
        kind: 'placeholder' as const,
        x: 0,
        y: 0,
        width: 10,
        height: 10,
        gen: { jobId: 'j1', message: '生成中…' },
      },
      b: {
        id: 'b',
        kind: 'placeholder' as const,
        x: 0,
        y: 0,
        width: 10,
        height: 10,
        gen: { jobId: '', message: '准备中' },
      },
      c: { id: 'c', kind: 'image', x: 0, y: 0, width: 10, height: 10 },
      d: {
        id: 'd',
        kind: 'placeholder' as const,
        x: 0,
        y: 0,
        width: 10,
        height: 10,
        gen: { jobId: 'local-x', message: '正在抠图' },
      },
    } as Record<string, ReconcileObj>,
  }
  const changed = markDisconnected(doc)
  // 本地任务(抠图合成等)不走 SSE,断线不改写其文案
  assert.deepEqual(changed, ['a'])
  const d = doc.objects.d as ReconcileObj
  assert.equal(d.gen?.message, '正在抠图')
  const a = doc.objects.a as ReconcileObj & { gen: { message: string } }
  const b = doc.objects.b as ReconcileObj & { gen: { message: string } }
  assert.equal(a.gen.message, '连接中断,正在查询')
  assert.equal(b.gen.message, '准备中')
  // 幂等:重复断线事件不反复改写
  assert.deepEqual(markDisconnected(doc), [])
})

interface PendingDoc {
  id: string
  order: string[]
  groups: Record<string, { title: string; prompt: string; slots: string[] }>
  objects: Record<string, ReconcileObj & { pendingConfirm?: boolean }>
}

function fakeStore(doc: PendingDoc) {
  return {
    doc,
    apply: (cmd: never) => applyCommand(doc, cmd),
    mutateTransient: (fn: (d: PendingDoc) => unknown) => fn(doc),
  }
}

test('confirmPendingGroup:服务端已接受 → 清除待确认标记并交还整组流程', async () => {
  const doc: PendingDoc = {
    order: [],
    id: 'd1',
    groups: { g1: { title: 'T', prompt: 'P', slots: ['s0', 's1', 's2', 's3'] } },
    objects: {
      s0: {
        id: 's0',
        kind: 'placeholder' as const,
        x: 0,
        y: 0,
        width: 1,
        height: 1,
        groupId: 'g1',
        pendingConfirm: true,
        gen: { jobId: '', message: '结果待确认' },
      },
      s1: {
        id: 's1',
        kind: 'placeholder' as const,
        x: 0,
        y: 0,
        width: 1,
        height: 1,
        groupId: 'g1',
        pendingConfirm: true,
        gen: { jobId: '', message: '结果待确认' },
      },
      s2: {
        id: 's2',
        kind: 'placeholder' as const,
        x: 0,
        y: 0,
        width: 1,
        height: 1,
        groupId: 'g1',
        pendingConfirm: true,
        gen: { jobId: '', message: '结果待确认' },
      },
      s3: {
        id: 's3',
        kind: 'placeholder' as const,
        x: 0,
        y: 0,
        width: 1,
        height: 1,
        groupId: 'g1',
        pendingConfirm: true,
        gen: { jobId: '', message: '结果待确认' },
      },
    },
  }
  const jobs = [
    { id: 'j0', status: 'running', clientRef: 's0', groupId: 'srv-g1' },
    { id: 'j1', status: 'running', clientRef: 's1', groupId: 'srv-g1' },
  ]
  const fetchImpl = (async () => ({ json: async () => ({ jobs }) })) as unknown as typeof fetch
  const result = await confirmPendingGroup({
    docStore: fakeStore(doc),
    groupId: 'g1',
    fetchImpl,
  })
  assert.equal(result.found, true)
  const s0 = doc.objects.s0
  const s3 = doc.objects.s3
  assert.equal(s0.pendingConfirm, false)
  assert.equal(s0.gen?.message, '准备中')
  assert.equal(s3.pendingConfirm, false)
  // 占位全部保留
  assert.equal(Object.keys(doc.objects).length, 4)
})

test('confirmPendingGroup:确认未入队 → 清理占位、保留组元数据可重试', async () => {
  const doc: PendingDoc = {
    order: [],
    id: 'd1',
    groups: { g1: { title: 'T', prompt: 'P', slots: ['s0', 's1'] } },
    objects: {
      s0: {
        id: 's0',
        kind: 'placeholder' as const,
        x: 0,
        y: 0,
        width: 1,
        height: 1,
        groupId: 'g1',
        pendingConfirm: true,
        gen: { jobId: '', message: '结果待确认' },
      },
      s1: {
        id: 's1',
        kind: 'placeholder' as const,
        x: 0,
        y: 0,
        width: 1,
        height: 1,
        groupId: 'g1',
        pendingConfirm: true,
        gen: { jobId: '', message: '结果待确认' },
      },
    },
  }
  const fetchImpl = (async () => ({ json: async () => ({ jobs: [] }) })) as unknown as typeof fetch
  const result = await confirmPendingGroup({
    docStore: fakeStore(doc),
    groupId: 'g1',
    fetchImpl,
  })
  assert.equal(result.found, false)
  assert.deepEqual(doc.objects, {})
  // 组元数据保留:"另生成一组"仍可用
  assert.equal(doc.groups.g1.prompt, 'P')
})

test('confirmPendingGroup:查询网络失败 → 不清理、可再次查询', async () => {
  const doc: PendingDoc = {
    order: [],
    id: 'd1',
    groups: { g1: { title: 'T', prompt: 'P', slots: ['s0'] } },
    objects: {
      s0: {
        id: 's0',
        kind: 'placeholder' as const,
        x: 0,
        y: 0,
        width: 1,
        height: 1,
        groupId: 'g1',
        pendingConfirm: true,
        gen: { jobId: '' },
      },
    },
  }
  const fetchImpl = (async () => {
    throw new TypeError('network down')
  }) as unknown as typeof fetch
  const result = await confirmPendingGroup({
    docStore: fakeStore(doc),
    groupId: 'g1',
    fetchImpl,
  })
  assert.equal(result.ok, false)
  assert.equal(Object.keys(doc.objects).length, 1)
})
