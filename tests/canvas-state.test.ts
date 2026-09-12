import test from 'node:test'
import assert from 'node:assert/strict'
import {
  cmdAddObjects,
  cmdMoveObjects,
  cmdUpdateObject,
  applyCommand,
  invertCommand,
  canCoalesce,
  coalesceInto,
} from '../apps/web/canvas/state/commands.js'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'
import { createAutoSaver } from '../apps/web/canvas/state/autosave.js'

type ObjKind = 'image' | 'video' | 'placeholder' | 'error'
const obj = (id: string, x = 0, y = 0, kind: ObjKind = 'image') => ({
  id,
  kind,
  x,
  y,
  width: 256,
  height: 256,
})
/* eslint-disable @typescript-eslint/no-explicit-any -- 测试快照结构宽松 */
const emptyDoc = () => ({
  id: 'doc-1',
  name: 't',
  objects: {} as Record<string, any>,
  order: [] as string[],
})

/* ---------- commands 纯函数 ---------- */

test('addObjects / removeObjects 互逆且维护 order', () => {
  const doc = emptyDoc()
  const add = cmdAddObjects([obj('a'), obj('b')])
  applyCommand(doc, add)
  assert.deepEqual(doc.order, ['a', 'b'])
  applyCommand(doc, invertCommand(add))
  assert.deepEqual(doc.order, [])
  assert.deepEqual(doc.objects, {})
})

test('moveObjects 逆命令恢复原位', () => {
  const doc = emptyDoc()
  applyCommand(doc, cmdAddObjects([obj('a', 10, 20)]))
  const move = cmdMoveObjects([{ id: 'a', from: { x: 10, y: 20 }, to: { x: 99, y: 88 } }])
  applyCommand(doc, move)
  assert.equal(doc.objects.a.x, 99)
  applyCommand(doc, invertCommand(move))
  assert.equal(doc.objects.a.x, 10)
  assert.equal(doc.objects.a.y, 20)
})

test('updateObject 前后 patch 互逆;对象缺失抛错', () => {
  const doc = emptyDoc()
  applyCommand(doc, cmdAddObjects([obj('a')]))
  const upd = cmdUpdateObject('a', { kind: 'error' }, { kind: 'image' })
  applyCommand(doc, upd)
  assert.equal(doc.objects.a.kind, 'error')
  applyCommand(doc, invertCommand(upd))
  assert.equal(doc.objects.a.kind, 'image')
  assert.throws(() => applyCommand(doc, cmdUpdateObject('nope', {}, {})), /对象不存在/)
})

test('canCoalesce:同对象组 move 可合并,不同组/不同类型不可', () => {
  const m1 = cmdMoveObjects([{ id: 'a', from: { x: 0, y: 0 }, to: { x: 1, y: 1 } }])
  const m2 = cmdMoveObjects([{ id: 'a', from: { x: 1, y: 1 }, to: { x: 2, y: 2 } }])
  const m3 = cmdMoveObjects([{ id: 'b', from: { x: 0, y: 0 }, to: { x: 1, y: 1 } }])
  assert.equal(canCoalesce(m1, m2), true)
  assert.equal(canCoalesce(m1, m3), false)
  assert.equal(canCoalesce(cmdAddObjects([obj('a')]), m1), false)

  coalesceInto(m1, m2)
  assert.deepEqual(m1.moves[0].to, { x: 2, y: 2 })
  assert.deepEqual(m1.moves[0].from, { x: 0, y: 0 })
})

/* ---------- DocStore ---------- */

test('apply/undo/redo 全链路;redo 栈被新命令清空', () => {
  const store = createDocStore(emptyDoc())
  store.apply(cmdAddObjects([obj('a')]))
  store.apply(cmdAddObjects([obj('b')]))
  assert.equal(store.canUndo(), true)

  assert.equal(store.undo(), true)
  assert.deepEqual(store.doc.order, ['a'])
  assert.equal(store.redo(), true)
  assert.deepEqual(store.doc.order, ['a', 'b'])

  store.undo()
  store.apply(cmdAddObjects([obj('c')]))
  assert.equal(store.canRedo(), false, '新命令应清空 redo 栈')
  assert.deepEqual(store.doc.order, ['a', 'c'])
})

test('窗口内同组 move 合并为一条 undo', async () => {
  const store = createDocStore(emptyDoc())
  store.apply(cmdAddObjects([obj('a')]))
  const events: string[] = []
  store.subscribe((e) => events.push(e.type))

  const move = (to: { x: number; y: number }, from: { x: number; y: number }) =>
    store.apply(cmdMoveObjects([{ id: 'a', from, to }]), { coalesce: true })
  move({ x: 10, y: 10 }, { x: 0, y: 0 })
  move({ x: 20, y: 20 }, { x: 10, y: 10 })
  move({ x: 30, y: 30 }, { x: 20, y: 20 })

  assert.equal(store.doc.objects.a.x, 30)
  assert.equal(store.undo(), true)
  assert.deepEqual(
    { x: store.doc.objects.a.x, y: store.doc.objects.a.y },
    { x: 0, y: 0 },
    '合并后一步撤回到起点',
  )
  assert.deepEqual(events, ['command', 'command', 'command', 'command'])
})

test('mutateTransient 不进 undo 栈(SSE 落位语义)', () => {
  const store = createDocStore(emptyDoc())
  store.apply(cmdAddObjects([{ ...obj('p'), kind: 'placeholder', gen: { jobId: 'j1' } }]))
  const types: string[] = []
  store.subscribe((e) => types.push(e.type))

  store.mutateTransient((doc) => {
    doc.objects.p.kind = 'image'
    doc.objects.p.assetId = 'asset-x'
    delete doc.objects.p.gen
  })
  assert.equal(store.doc.objects.p.kind, 'image')

  // 瞬态变更不产生 undo 条目:一次 undo 撤销的是"创建占位"命令(占位随之移除)
  assert.equal(store.undo(), true)
  assert.equal(store.doc.objects.p, undefined)
  assert.deepEqual(store.doc.order, [])

  // redo 恢复命令携带的原始占位;瞬态变更不回放(PRD §7.2)
  assert.equal(store.redo(), true)
  assert.equal((store.doc.objects.p as { kind: string } | undefined)?.kind, 'placeholder')
  // 订阅晚于首个 apply:只应看到 transient 与 undo/redo 的 command
  assert.deepEqual(types, ['transient', 'command', 'command'])
})

test('初始文档被浅拷贝,外部改动不泄漏进 store', () => {
  const initial = emptyDoc()
  initial.objects.a = obj('a')
  const store = createDocStore(initial)
  delete initial.objects.a
  initial.order.push('zz')
  assert.ok(store.doc.objects.a)
  assert.deepEqual(store.doc.order, [])
})

/* ---------- AutoSaver(依赖注入) ---------- */

test('autoSaver:防抖后全量 POST;unload 走 beacon;串行化排队', async () => {
  const saved: { url: string; body: any }[] = []
  const beacons: { url: string; body: any }[] = []
  const statuses: string[] = []
  // 门控:gate() 后的下一个请求挂起,直到 release() 放行
  let gated = false
  const waiting: Array<() => void> = []
  const gate = () => {
    gated = true
  }
  const release = () => {
    gated = false
    while (waiting.length) {
      waiting.shift()?.()
    }
  }
  const fetchImpl = (async (url: string, init: RequestInit) => {
    if (gated && !init.keepalive) {
      await new Promise<void>((r) => waiting.push(r))
    }
    saved.push({ url, body: JSON.parse(String(init.body)) })
    return new Response('{}', { status: 200 })
  }) as typeof fetch
  const beaconImpl = (url: string, blob: Blob) => {
    blob.text().then((t) => beacons.push({ url, body: JSON.parse(t) }))
    return true
  }

  const doc = emptyDoc()
  const saver = createAutoSaver({
    docId: doc.id,
    getDoc: () => doc,
    fetchImpl,
    beaconImpl,
    onStatus: (s: string) => statuses.push(s),
    debounceMs: 10,
  })

  doc.objects.a = obj('a')
  saver.markDirty()
  saver.markDirty()
  await new Promise((r) => setTimeout(r, 40))
  assert.equal(saved.length, 1, '两次 markDirty 防抖合并为一次保存')
  assert.ok(saved[0].body.objects.a)

  // 保存进行中(gate 挂起)再次 markDirty:完成后应排队补一次,携带最新快照
  gate()
  doc.objects.b = obj('b')
  saver.markDirty()
  await new Promise((r) => setTimeout(r, 30))
  assert.equal(saved.length, 1, '挂起的请求尚未完成,仍只有第一次保存')
  doc.objects.c = obj('c')
  saver.markDirty()
  await new Promise((r) => setTimeout(r, 30))
  release()
  await new Promise((r) => setTimeout(r, 30))
  assert.equal(saved.length, 3, '挂起完成后排队补发,共三次保存')
  assert.ok(saved[1].body.objects.b, '第二次保存携带当时快照')
  assert.ok(saved[2].body.objects.c, '排队保存携带最新快照')

  doc.objects.d = obj('d')
  saver.flushOnUnload()
  await new Promise((r) => setTimeout(r, 10))
  assert.equal(beacons.length, 1)
  assert.ok(beacons[0].body.objects.d)
  assert.ok(statuses.includes('saving') && statuses.includes('saved'))
})
