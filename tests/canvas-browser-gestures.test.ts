import './dom.js'
import assert from 'node:assert/strict'
import test from 'node:test'
import { preventBrowserGestureZoom } from '../apps/web/canvas/renderer-konva/browser-gestures.js'

test('画布挂载期间拦截页面缩放，保留滚动和事件传递，卸载时恢复', () => {
  const dispose = preventBrowserGestureZoom(document)
  try {
    const scroll = new window.WheelEvent('wheel', { cancelable: true, deltaY: 20 })
    document.dispatchEvent(scroll)
    assert.equal(scroll.defaultPrevented, false)
    const pinch = new window.WheelEvent('wheel', { cancelable: true, ctrlKey: true, deltaY: -2 })
    let received = false
    const receive = () => {
      received = true
    }
    document.addEventListener('wheel', receive, { once: true })
    document.dispatchEvent(pinch)
    assert.equal(pinch.defaultPrevented, true)
    assert.equal(received, true, '画布仍可接收并处理捏合事件')
    for (const type of ['gesturestart', 'gesturechange', 'gestureend']) {
      const gesture = new window.Event(type, { cancelable: true })
      document.dispatchEvent(gesture)
      assert.equal(gesture.defaultPrevented, true)
    }
  } finally {
    dispose()
  }
  const pinch = new window.WheelEvent('wheel', { cancelable: true, ctrlKey: true })
  document.dispatchEvent(pinch)
  assert.equal(pinch.defaultPrevented, false)
  const gesture = new window.Event('gesturechange', { cancelable: true })
  document.dispatchEvent(gesture)
  assert.equal(gesture.defaultPrevented, false)
})
