import assert from 'node:assert/strict'
import test from 'node:test'

import { createCanvasView } from '../apps/web/canvas/core/canvas-view.js'
import type { CanvasInputEvent, CanvasModifiers } from '../apps/web/canvas/core/types.js'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'

const modifiers: CanvasModifiers = {
  shift: false,
  alt: false,
  ctrl: false,
  meta: false,
  space: false,
}

function pointer(
  type: Extract<CanvasInputEvent, { screenPoint: unknown }>['type'],
  x: number,
  y: number,
  target: Extract<CanvasInputEvent, { screenPoint: unknown }>['target'] = { kind: 'canvas' },
): Extract<CanvasInputEvent, { screenPoint: unknown }> {
  return {
    type,
    pointerId: 1,
    button: 0,
    screenPoint: { x, y },
    worldPoint: { x, y },
    modifiers,
    target,
  }
}

function key(
  value: string,
  options: Partial<CanvasModifiers> = {},
): Extract<CanvasInputEvent, { key: string }> {
  return {
    type: 'key-down',
    key: value,
    code: value === ' ' ? 'Space' : `Key${value.toUpperCase()}`,
    modifiers: { ...modifiers, ...options },
  }
}

function setup(readonly = false) {
  const store = createDocStore({
    id: 'doc-1',
    name: 'Test',
    objects: {
      one: { id: 'one', kind: 'image', x: 10, y: 20, width: 100, height: 80 },
      two: { id: 'two', kind: 'text', x: 200, y: 200, width: 80, height: 40, text: 'Hi' },
    },
    order: ['one', 'two'],
  })
  return {
    store,
    view: createCanvasView({ store, readonly, viewport: { width: 800, height: 600 } }),
  }
}

test('双指滚动在所有方向平移画布并保持缩放比例', () => {
  const { view } = setup()
  const initial = view.getSnapshot().camera
  for (const delta of [
    { x: 0, y: 60 },
    { x: 40, y: 0 },
    { x: -20, y: -80 },
  ]) {
    const before = view.getSnapshot().camera
    view.dispatchInput({ ...pointer('wheel', 100, 100), wheelDelta: delta })
    assert.deepEqual(view.getSnapshot().camera, {
      x: before.x + delta.x / before.scale,
      y: before.y + delta.y / before.scale,
      scale: initial.scale,
    })
  }
  view.destroy()
})

test('捏合或 Ctrl 滚动仍围绕指针缩放画布', () => {
  const { view } = setup()
  const screenPoint = { x: 100, y: 100 }
  const worldPoint = view.screenToWorld(screenPoint)
  const before = view.getSnapshot().camera.scale
  view.dispatchInput({
    ...pointer('wheel', 100, 100),
    worldPoint,
    modifiers: { ...modifiers, ctrl: true },
    wheelDelta: { x: 0, y: -60 },
  })
  assert.ok(view.getSnapshot().camera.scale > before)
  assert.deepEqual(view.screenToWorld(screenPoint), worldPoint)
  view.destroy()
})

test('CanvasView 通过 Layer/Mono 将拖动提交为单条 Command', () => {
  const { store, view } = setup()
  view.dispatchInput(pointer('lbutton-down', 20, 30, { kind: 'object', objectId: 'one' }))
  view.dispatchInput(pointer('move', 50, 70, { kind: 'object', objectId: 'one' }))

  assert.deepEqual(view.getSnapshot().objectPreviews.one, { x: 40, y: 60 })
  assert.equal(store.doc.objects.one.x, 10, '拖动中不写文档')

  view.dispatchInput(pointer('lbutton-up', 50, 70, { kind: 'object', objectId: 'one' }))
  assert.equal(store.doc.objects.one.x, 40)
  assert.equal(store.doc.objects.one.y, 60)
  assert.equal(view.getSnapshot().objectPreviews.one, undefined)

  assert.equal(view.undo(), true)
  assert.equal(store.doc.objects.one.x, 10)
  assert.equal(store.doc.objects.one.y, 20)
  view.destroy()
})

test('只读 CanvasView 允许选择但不会创建移动 Mono', () => {
  const { store, view } = setup(true)
  view.dispatchInput(pointer('lbutton-down', 20, 30, { kind: 'object', objectId: 'one' }))
  view.dispatchInput(pointer('move', 80, 90, { kind: 'object', objectId: 'one' }))
  view.dispatchInput(pointer('lbutton-up', 80, 90, { kind: 'object', objectId: 'one' }))

  assert.deepEqual(view.getSnapshot().selection, ['one'])
  assert.equal(view.getSnapshot().effectiveLayer, 'readonly-select')
  assert.equal(store.doc.objects.one.x, 10)
  view.destroy()
})

test('CanvasView 将 generation entity 投影为可渲染产物，不改写 placement', () => {
  const store = createDocStore({
    id: 'generated-doc',
    name: 'Generated',
    objects: {
      generated: {
        id: 'generated',
        kind: 'placeholder',
        generationId: 'generation-1',
        x: 10,
        y: 20,
        width: 100,
        height: 80,
      },
    },
    order: ['generated'],
    generations: {
      'generation-1': {
        id: 'generation-1',
        operationId: 'operation-1',
        spec: { mode: 'text-to-image', params: { prompt: '猫' } },
        status: 'completed',
        result: { kind: 'image', src: '/images/cat.png', ext: 'png', imageId: 'image-1' },
        version: 2,
      },
    },
  })
  const view = createCanvasView({ store })

  assert.equal(store.doc.objects.generated.kind, 'placeholder')
  assert.equal(view.getDocument().objects.generated.kind, 'image')
  assert.equal(view.getDocument().objects.generated.imageId, 'image-1')
  view.destroy()
})

test('Layer 内部右键请求经 CanvasView 发布给上层', () => {
  const { view } = setup()
  let received: unknown = null
  view.on('context-menu:requested', (event) => {
    received = event
  })

  view.dispatchInput(pointer('context-menu', 25, 35, { kind: 'object', objectId: 'one' }))
  assert.deepEqual(received, {
    target: { kind: 'object', objectId: 'one' },
    screenPoint: { x: 25, y: 35 },
    worldPoint: { x: 25, y: 35 },
  })
  view.destroy()
})

test('框选只更新 CanvasView 选择状态', () => {
  const { view } = setup()
  view.dispatchInput(pointer('lbutton-down', 0, 0))
  view.dispatchInput(pointer('move', 150, 150))
  assert.deepEqual(view.getSnapshot().marquee, { x: 0, y: 0, width: 150, height: 150 })
  view.dispatchInput(pointer('lbutton-up', 150, 150))
  assert.deepEqual(view.getSnapshot().selection, ['one'])
  assert.equal(view.getSnapshot().marquee, null)
  view.destroy()
})

test('连线 Mono 只对外发布语义请求，不直接修改文档', () => {
  const { store, view } = setup()
  let request: { sourceId: string; targetId: string } | null = null
  view.on('connection:requested', (payload) => {
    request = payload
  })
  view.activateTool('connect')
  view.dispatchInput(
    pointer('lbutton-down', 110, 60, { kind: 'port', objectId: 'one', port: 'output' }),
  )
  view.dispatchInput(pointer('move', 200, 220, { kind: 'canvas' }))
  assert.deepEqual(view.getSnapshot().connectionPreview?.end, { x: 200, y: 220 })
  view.dispatchInput(
    pointer('lbutton-up', 200, 220, { kind: 'port', objectId: 'two', port: 'input' }),
  )
  assert.deepEqual(request, { sourceId: 'one', targetId: 'two' })
  assert.equal(view.getSnapshot().connectionPreview, null)
  assert.equal(store.canUndo(), false)
  view.destroy()
})

test('文字 Layer 创建对象后通过 CanvasView 请求 DOM 编辑器', () => {
  const { store, view } = setup()
  const editRequests: { objectId: string; screenRect: unknown }[] = []
  view.on('text-edit:requested', (payload) => {
    editRequests.push(payload)
  })
  view.activateTool('text')
  view.dispatchInput(pointer('lbutton-down', 300, 100))
  view.dispatchInput(pointer('lbutton-up', 420, 140))

  const created = store.doc.objects[store.doc.order.at(-1) ?? '']
  assert.equal(created?.kind, 'text')
  assert.deepEqual(view.getSnapshot().selection, [created?.id])
  assert.equal(editRequests[0]?.objectId, created?.id)
  assert.equal(store.canUndo(), true)
  view.undo()
  assert.equal(store.doc.objects[created.id], undefined)
  view.destroy()
})

test('蒙版笔画高频数据留在 Snapshot，结束时才流向上层', () => {
  const { store, view } = setup()
  const committed: Array<readonly { x: number; y: number }[]> = []
  view.on('mask:stroke-committed', ({ points }) => committed.push(points))
  view.select(['one'])
  view.activateTool('mask')
  view.dispatchInput(pointer('lbutton-down', 20, 30, { kind: 'object', objectId: 'one' }))
  view.dispatchInput(pointer('move', 24, 35, { kind: 'object', objectId: 'one' }))
  assert.equal(view.getSnapshot().maskStroke?.length, 2)
  assert.equal(committed.length, 0)
  view.dispatchInput(pointer('lbutton-up', 28, 40, { kind: 'object', objectId: 'one' }))
  assert.equal(view.getSnapshot().maskStroke, null)
  assert.equal(committed[0]?.length, 3)
  assert.equal(store.canUndo(), false)
  view.destroy()
})

test('局部重绘区域只在手势完成后发布', () => {
  const { view } = setup()
  const regions: { x: number; y: number; width: number; height: number }[] = []
  view.on('inpaint:region-committed', ({ region }) => regions.push(region))
  view.activateTool('inpaint')
  view.dispatchInput(pointer('lbutton-down', 20, 30, { kind: 'object', objectId: 'one' }))
  view.dispatchInput(pointer('move', 80, 90, { kind: 'object', objectId: 'one' }))
  assert.deepEqual(view.getSnapshot().inpaintRegion, { x: 20, y: 30, width: 60, height: 60 })
  assert.equal(regions.length, 0)
  view.dispatchInput(pointer('lbutton-up', 80, 90, { kind: 'object', objectId: 'one' }))
  assert.deepEqual(regions, [{ x: 20, y: 30, width: 60, height: 60 }])
  assert.equal(view.getSnapshot().inpaintRegion, null)
  view.destroy()
})

test('快捷键通过 CanvasView 执行全选、复制和撤销', () => {
  const { store, view } = setup()
  view.dispatchInput(key('a', { meta: true }))
  assert.deepEqual(view.getSnapshot().selection, ['one', 'two'])
  view.dispatchInput(key('d', { meta: true }))
  assert.equal(store.doc.order.length, 4)
  view.dispatchInput(key('z', { meta: true }))
  assert.equal(store.doc.order.length, 2)
  view.destroy()
})

test('Shift 点击切换多选成员', () => {
  const { view } = setup()
  view.select(['one'])
  const event = pointer('lbutton-down', 210, 210, { kind: 'object', objectId: 'two' })
  event.modifiers = { ...modifiers, shift: true }
  view.dispatchInput(event)
  view.dispatchInput(pointer('lbutton-up', 210, 210, { kind: 'object', objectId: 'two' }))
  assert.deepEqual(view.getSnapshot().selection, ['one', 'two'])
  view.destroy()
})

test('连续平移中混入 Ctrl wheel 不切换缩放，停顿后可开始捏合', () => {
  const { view } = setup()
  const send = (time: number, ctrl: boolean, y: number) =>
    view.dispatchInput({
      ...pointer('wheel', 100, 100),
      worldPoint: view.screenToWorld({ x: 100, y: 100 }),
      modifiers: { ...modifiers, ctrl },
      wheelTime: time,
      wheelDelta: { x: 0, y },
    })
  send(0, false, 20)
  send(16, true, -0.1)
  send(32, false, -30)
  assert.equal(view.getSnapshot().camera.scale, 1)
  send(400, true, 0)
  assert.equal(view.getSnapshot().camera.scale, 1)
  send(416, true, -0.1)
  assert.ok(view.getSnapshot().camera.scale > 1)
  assert.ok(view.getSnapshot().camera.scale < 1.001, '微小捏合不会跳变 10%')
  view.destroy()
})

test('抠图笔刷按下时固定模式与世界宽度，撤销重做不影响文档历史', () => {
  const { store, view } = setup()
  view.select(['one'])
  view.activateTool('mask')
  view.setMaskBrush({ mode: 'erase', size: 40 })
  const target = { kind: 'object', objectId: 'one' } as const
  view.dispatchInput(pointer('lbutton-down', 20, 30, target))
  view.dispatchInput(pointer('lbutton-up', 30, 40, target))
  assert.equal(view.getSnapshot().maskEdits[0].mode, 'erase')
  assert.equal(view.getSnapshot().maskEdits[0].width, 40 / view.getSnapshot().camera.scale)
  view.dispatchInput(key('z', { meta: true }))
  assert.equal(view.getSnapshot().maskEdits.length, 0)
  assert.equal(view.getSnapshot().canRedoMask, true)
  view.dispatchInput(key('z', { meta: true, shift: true }))
  assert.equal(view.getSnapshot().maskEdits.length, 1)
  assert.equal(store.canUndo(), false)
  view.undoMaskStroke()
  view.setMaskBrush({ mode: 'paint', size: 12 })
  view.dispatchInput(pointer('lbutton-down', 20, 30, target))
  view.dispatchInput(pointer('lbutton-up', 30, 40, target))
  assert.equal(view.getSnapshot().canRedoMask, false)
  assert.equal(view.getSnapshot().maskEdits[0].mode, 'paint')
  view.activateTool('select')
  assert.equal(view.getSnapshot().maskEdits.length, 0)
  assert.equal(view.getSnapshot().canRedoMask, false)
  view.destroy()
})

test('蒙版模式点击其他对象只切换选择，不把笔画画到其他对象', () => {
  const { view } = setup()
  view.select(['one'])
  view.activateTool('mask')
  view.dispatchInput(pointer('lbutton-down', 220, 220, { kind: 'object', objectId: 'two' }))
  assert.deepEqual(view.getSnapshot().selection, ['two'])
  assert.equal(view.getSnapshot().maskEdits.length, 0)
  view.destroy()
})
