import './dom.js'
import assert from 'node:assert/strict'
import test, { afterEach } from 'node:test'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import {
  ContentToolbar,
  ViewportControls,
  insertCanvasPlaceholder,
} from '../apps/web/canvas/studio-controls.js'
import { createCanvasView } from '../apps/web/canvas/core/canvas-view.js'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'

afterEach(cleanup)

test('左栏图片、视频、时间线和内容入口均连接独立操作', () => {
  const actions: string[] = []
  render(
    <ContentToolbar
      onImage={() => actions.push('image')}
      onVideo={() => actions.push('video')}
      onTimeline={() => actions.push('timeline')}
      onLibrary={() => actions.push('library')}
      onBoard={() => actions.push('board')}
      onText={() => actions.push('text')}
    />,
  )
  const rail = screen.getByRole('navigation', { name: '添加内容' })
  const buttons = within(rail).getAllByRole('button')
  assert.deepEqual(
    buttons.map((button) => button.getAttribute('aria-label')),
    ['图片', '视频', '时间线', '素材库', '画板', '文字'],
  )
  for (const button of buttons) {
    fireEvent.click(button)
  }
  assert.deepEqual(actions, ['image', 'video', 'timeline', 'library', 'board', 'text'])
})

test('原版缩放入口连接当前 CanvasView，切换拖动和缩放不修改画布文档', () => {
  const store = createDocStore({ id: 'doc', name: '原版画布', objects: {}, order: [] })
  const view = createCanvasView({ store, viewport: { width: 1000, height: 800 } })
  const before = structuredClone(store.doc)
  const rendered = render(<ViewportControls view={view} tool="select" scale={1} />)
  fireEvent.click(screen.getByRole('button', { name: '切换为拖动工具' }))
  assert.equal(view.getSnapshot().requestedTool, 'pan')
  rendered.rerender(<ViewportControls view={view} tool="pan" scale={1} />)
  fireEvent.click(screen.getByRole('button', { name: '切换为选择工具' }))
  assert.equal(view.getSnapshot().requestedTool, 'select')
  const details = rendered.container.querySelector('details')
  assert.ok(details)
  details.open = true
  fireEvent.click(screen.getByRole('button', { name: '50%' }))
  assert.equal(view.getSnapshot().camera.scale, 0.5)
  fireEvent.click(screen.getByRole('button', { name: '200%' }))
  assert.equal(view.getSnapshot().camera.scale, 2)
  assert.deepEqual(store.doc, before)
  view.destroy()
})

test('图片、视频、时间线插入不同空占位符并支持撤销', () => {
  const store = createDocStore({ id: 'placeholders', name: '占位符', objects: {}, order: [] })
  const view = createCanvasView({ store, viewport: { width: 1000, height: 800 } })
  for (const type of ['image', 'video', 'timeline'] as const) {
    insertCanvasPlaceholder(view, type)
    const object = store.doc.objects[view.getSnapshot().selection[0]]
    assert.equal(object.kind, 'placeholder')
    assert.equal(object.placeholderType, type)
    assert.equal(object.src, undefined)
    assert.equal(object.gen, undefined)
  }
  assert.equal(store.doc.order.length, 3)
  view.undo()
  assert.equal(store.doc.order.length, 2)
  view.destroy()
})
