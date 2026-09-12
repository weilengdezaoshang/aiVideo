import test from 'node:test'
import assert from 'node:assert/strict'
import { createInteraction, bindInteraction } from '../apps/web/canvas/engine/interaction.js'

test('先按鼠标再按空格：原操作收尾后才能平移', () => {
  const state = createInteraction()
  assert.equal(state.begin(0), 'select')
  state.setSpace(true)
  assert.equal(state.getState().effective, 'select')
  state.end()
  assert.equal(state.getState().effective, 'pan')
  assert.equal(state.begin(0), 'pan')
  state.setSpace(false)
  assert.equal(state.getState().effective, 'pan')
  state.end()
  assert.equal(state.getState().effective, 'select')
})

test('蒙版只接左键，识别允许导航但拒绝编辑，模式切换清除临时平移', () => {
  const state = createInteraction()
  state.setSpace(true)
  state.setMode('mask', 'image-1')
  state.setSpace(true)
  assert.equal(state.getState().effective, 'mask')
  assert.equal(state.begin(1), null)
  assert.equal(state.begin(2), null)
  assert.equal(state.begin(0), 'mask')
  assert.equal(state.canEdit(), false)
  assert.equal(state.canNavigate(), false)
  state.setMode('recognizing', 'image-1')
  assert.equal(state.getState().gesture, null)
  assert.equal(state.begin(0), null)
  assert.equal(state.canNavigate(), true)
  assert.equal(state.begin(1), 'pan')
  state.end()
  state.setSpace(true)
  assert.equal(state.begin(0), 'pan')
  state.end()
  state.setMode('select')
  assert.equal(state.getState().targetId, null)
  assert.equal(state.getState().effective, 'select')
})

test('中键平移期间不会选择，取消后恢复普通模式', () => {
  const state = createInteraction()
  let cancelled = 0
  state.onCancel(() => cancelled++)
  state.begin(1)
  assert.equal(state.canEdit(), false)
  state.cancel()
  assert.equal(cancelled, 1)
  assert.equal(state.canEdit(), true)
})

test('统一入口：蒙版事件不穿透、点击其他对象转交选择、失焦清理、输入框保留快捷键', async () => {
  class FakeElement extends EventTarget {
    tag = 'canvas'
    isContentEditable = false
    captured = new Set<number>()
    matches(selector: string) {
      return selector.split(', ').includes(this.tag)
    }
    setPointerCapture(id: number) {
      this.captured.add(id)
    }
    hasPointerCapture(id: number) {
      return this.captured.has(id)
    }
    releasePointerCapture(id: number) {
      this.captured.delete(id)
    }
  }
  const win = new EventTarget()
  const doc = new EventTarget()
  const surface = new FakeElement()
  const previous = ['window', 'document', 'HTMLElement'].map((k) =>
    Object.getOwnPropertyDescriptor(globalThis, k),
  )
  for (const [key, value] of Object.entries({
    window: win,
    document: doc,
    HTMLElement: FakeElement,
  })) {
    Object.defineProperty(globalThis, key, { configurable: true, value })
  }
  const state = createInteraction()
  const calls: string[] = []
  const router = bindInteraction(surface as unknown as HTMLElement, state, {
    pan: () => calls.push('pan'),
    wheel: () => calls.push('wheel'),
  })
  function send(
    type: string,
    extra: Record<string, unknown> = {},
    path: EventTarget[] = [surface],
  ) {
    const event = new Event(type, { cancelable: true })
    Object.defineProperty(event, 'composedPath', { value: () => path })
    for (const [key, value] of Object.entries({
      pointerId: 1,
      button: 0,
      clientX: 20,
      clientY: 20,
      key: '',
      ...extra,
    })) {
      Object.defineProperty(event, key, { value })
    }
    win.dispatchEvent(event)
    return event
  }
  try {
    state.setMode('mask', 'source')
    router.setMaskHandlers({
      down: () => calls.push('down'),
      move: () => calls.push('move'),
      up: () => calls.push('up'),
      cancel: () => calls.push('cancel'),
      key: () => calls.push('mask-key'),
    })
    router.setSelectionKey(() => calls.push('selection-key'))
    let leaked = 0
    win.addEventListener('mousedown', () => leaked++)
    send('pointerdown')
    assert.equal(send('mousedown').defaultPrevented, true)
    send('pointermove')
    send('pointerup')
    assert.deepEqual(calls, ['down', 'move', 'up'])
    assert.equal(leaked, 0)
    assert.equal(surface.captured.size, 0)
    send('keydown', { key: 'z', ctrlKey: true })
    assert.equal(calls.at(-1), 'mask-key')
    assert.ok(!calls.includes('selection-key'))
    send('pointerdown')
    win.dispatchEvent(new Event('blur'))
    assert.equal(calls.at(-1), 'cancel')
    assert.equal(state.getState().gesture, null)
    assert.equal(surface.captured.size, 0)
    router.setMaskHandlers({
      beforeDown: () => {
        calls.push('select-other')
        state.setMode('select')
        router.setMaskHandlers(null)
      },
      down: () => calls.push('unexpected-paint'),
      move() {},
      up() {},
      cancel() {},
      key() {},
    })
    assert.equal(send('pointerdown').defaultPrevented, false)
    assert.equal(state.getState().gesture, 'select')
    assert.ok(!calls.includes('unexpected-paint'))
    assert.equal(send('mousedown').defaultPrevented, false)
    assert.equal(leaked, 1)
    send('pointerup')
    await new Promise((resolve) => setTimeout(resolve, 1))
    assert.equal(state.getState().gesture, null)
    state.setMode('mask', 'source')
    const input = new FakeElement()
    input.tag = 'input'
    assert.equal(send('keydown', { key: 'z', ctrlKey: true }, [input]).defaultPrevented, false)
    state.setMode('recognizing', 'source')
    router.setRecognitionCancel(() => calls.push('cancel-recognition'))
    assert.equal(send('keydown', { key: 'Delete' }).defaultPrevented, true)
    send('keydown', { key: 'Escape' })
    assert.equal(calls.at(-1), 'cancel-recognition')
    router.setRecognitionPointer(() => calls.push('source-click'))
    assert.equal(send('pointerdown').defaultPrevented, true)
    assert.equal(state.getState().mode, 'recognizing')
    send('keydown', { key: ' ', code: 'Space' })
    assert.equal(send('pointerdown').defaultPrevented, true)
    assert.equal(state.getState().gesture, 'pan')
    send('pointermove')
    assert.equal(calls.at(-1), 'pan')
    send('pointerup')
    send('keyup', { code: 'Space' })
    router.setRecognitionPointer(() => {
      calls.push('cancel-and-select')
      state.setMode('select')
    })
    assert.equal(send('pointerdown', { button: 2 }).defaultPrevented, false)
    assert.equal(state.getState().mode, 'select')
    assert.equal(send('contextmenu').defaultPrevented, false)
    assert.equal(calls.at(-1), 'cancel-and-select')
  } finally {
    router.destroy()
    ;['window', 'document', 'HTMLElement'].forEach((key, i) => {
      const descriptor = previous[i]
      if (descriptor) {
        Object.defineProperty(globalThis, key, descriptor)
      } else {
        Reflect.deleteProperty(globalThis, key)
      }
    })
  }
})
