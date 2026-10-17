import './dom.js'
import React from 'react'
import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { cleanup, fireEvent, render } from '@testing-library/react'
import { MediaPreview } from '../apps/web/canvas/media-preview.js'
import type { CanvasObj } from '../apps/web/canvas/state/commands.js'

afterEach(cleanup)
window.HTMLDialogElement.prototype.showModal = function () {
  this.setAttribute('open', '')
}
const object: CanvasObj = {
  id: 'media',
  kind: 'image',
  src: '/media.png',
  name: '咖啡主视觉',
  x: 0,
  y: 0,
  width: 100,
  height: 100,
}
test('预览显示媒体原始尺寸而非画布尺寸，加载失败隐藏下载入口', () => {
  const view = render(<MediaPreview object={object} onClose={() => {}} />)
  const image = view.getByRole('img')
  Object.defineProperties(image, { naturalWidth: { value: 2048 }, naturalHeight: { value: 1536 } })
  fireEvent.load(image)
  assert.ok(view.getByText('2048 × 1536'))
  fireEvent.error(image)
  assert.ok(view.getByRole('alert'))
  assert.equal(view.queryByRole('link', { name: /下载/ }), null)
})
test('视频保留手动播放与内联控制，读取真实时长，关闭回到原入口', () => {
  const trigger = document.createElement('button')
  document.body.append(trigger)
  trigger.focus()
  let closed = false
  const view = render(
    <MediaPreview
      object={{ ...object, kind: 'video', src: '/media.mp4' }}
      onClose={() => {
        closed = true
      }}
    />,
  )
  const video = view.container.querySelector('video')!
  assert.equal(video.controls, true)
  assert.equal(video.autoplay, false)
  assert.equal(video.playsInline, true)
  Object.defineProperties(video, {
    videoWidth: { value: 1920 },
    videoHeight: { value: 1080 },
    duration: { value: 8.24 },
  })
  fireEvent.loadedMetadata(video)
  assert.ok(view.getByText('1920 × 1080'))
  assert.ok(view.getByText('8.2 秒'))
  fireEvent.click(view.getByRole('button', { name: /关闭预览/ }))
  assert.equal(closed, true)
  view.unmount()
  assert.equal(document.activeElement, trigger)
  trigger.remove()
})
