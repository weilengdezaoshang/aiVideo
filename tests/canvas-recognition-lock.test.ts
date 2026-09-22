import test from 'node:test'
import assert from 'node:assert/strict'
import {
  createRecognitionLock,
  recognitionPosition,
} from '../apps/web/canvas/flows/recognition-lock.js'

test('识别提示跟随图片可见区域，贴边避让且离屏回退居中', () => {
  const viewport = { width: 1000, height: 800 }
  const card = { width: 240, height: 60 }
  assert.deepEqual(
    recognitionPosition({ left: 100, top: 200, width: 400, height: 300 }, viewport, card),
    { left: 180, top: 320 },
  )
  assert.deepEqual(
    recognitionPosition({ left: -300, top: 0, width: 400, height: 100 }, viewport, card),
    { left: 12, top: 20 },
  )
  assert.deepEqual(
    recognitionPosition({ left: 1200, top: 0, width: 400, height: 100 }, viewport, card),
    { left: 380, top: 370 },
  )
  assert.deepEqual(recognitionPosition(null, { width: 320, height: 480 }, card), {
    left: 40,
    top: 210,
  })
})

test('识别提示取消和完成时关闭，重复解锁安全', () => {
  class Element extends EventTarget {
    hidden = true
    style = {}
    offsetWidth = 240
    offsetHeight = 60
    setAttribute() {}
    append() {}
  }
  const win = new EventTarget()
  Object.assign(win, { innerWidth: 1000, innerHeight: 800 })
  const dialog = new Element()
  const cancel = new Element()
  let frames = 0
  let created = 0
  const globals = ['window', 'document', 'requestAnimationFrame', 'cancelAnimationFrame'] as const
  const previous = globals.map((key) => Object.getOwnPropertyDescriptor(globalThis, key))
  Object.defineProperty(globalThis, 'window', { configurable: true, value: win })
  Object.defineProperty(globalThis, 'document', {
    configurable: true,
    value: {
      createElement: (tag: string) =>
        ++created === 1 ? dialog : tag === 'button' ? cancel : new Element(),
      body: new Element(),
    },
  })
  Object.defineProperty(globalThis, 'requestAnimationFrame', {
    configurable: true,
    value: () => ++frames,
  })
  Object.defineProperty(globalThis, 'cancelAnimationFrame', {
    configurable: true,
    value: () => {
      frames--
    },
  })
  try {
    let cancelled = 0
    const lock = createRecognitionLock({
      onCancel: () => {
        cancelled++
        lock.unlock()
      },
    })
    lock.lock()
    assert.equal(dialog.hidden, false)
    assert.equal(frames, 1)
    cancel.dispatchEvent(new Event('click'))
    assert.equal(cancelled, 1)
    assert.equal(dialog.hidden, true)
    assert.equal(frames, 0)
    lock.lock()
    lock.unlock()
    lock.unlock()
    assert.equal(dialog.hidden, true)
    assert.equal(frames, 0)
  } finally {
    globals.forEach((key, index) => {
      const descriptor = previous[index]
      if (descriptor) {
        Object.defineProperty(globalThis, key, descriptor)
      } else {
        Reflect.deleteProperty(globalThis, key)
      }
    })
  }
})
