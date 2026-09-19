import './dom.js'
import test from 'node:test'
import assert from 'node:assert/strict'
import { fireEvent, screen, waitFor } from '@testing-library/dom'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'
import { createTimelinePanel } from '../apps/web/canvas/flows/timeline-panel.js'

test('通过界面加入图片、延长时长、复制、撤销和删除，不改变画布素材', async () => {
  document.body.innerHTML = '<div class="topbar-tools"></div>'
  const originalPause = HTMLMediaElement.prototype.pause
  HTMLMediaElement.prototype.pause = () => {}
  const store = createDocStore({
    id: 'doc',
    name: '测试',
    order: ['image'],
    objects: {
      image: {
        id: 'image',
        kind: 'image',
        name: '产品图',
        x: 0,
        y: 0,
        width: 200,
        height: 200,
        src: '/images/product.png',
      },
    },
  })
  const panel = createTimelinePanel(store, () => ['image'])
  try {
    fireEvent.click(screen.getByRole('button', { name: '时间线' }))
    fireEvent.click(screen.getByRole('button', { name: '加入选中素材' }))
    await waitFor(() => assert.equal(store.doc.timeline?.clips.length, 1))
    fireEvent.click(screen.getByText('片段属性 · 时长 / 字幕 / 原声'))
    fireEvent.change(screen.getByLabelText('片段字幕'), { target: { value: '<产品>展示' } })
    assert.equal(store.doc.timeline?.clips[0].subtitle, '<产品>展示')
    assert.equal(screen.getByLabelText('当前片段字幕').textContent, '<产品>展示')
    assert.equal(screen.getByLabelText('当前片段字幕').children.length, 0)
    fireEvent.click(screen.getByRole('button', { name: '撤销' }))
    assert.equal(store.doc.timeline?.clips[0].subtitle, undefined)
    assert.equal(screen.getByLabelText('当前片段字幕').hidden, true)
    const out = screen.getByLabelText('片段出点（秒）')
    fireEvent.input(out, { target: { value: '8' } })
    fireEvent.click(screen.getByRole('button', { name: '应用裁剪' }))
    assert.equal(store.doc.timeline?.clips[0].outFrame, 240)
    fireEvent.click(screen.getByRole('button', { name: '复制' }))
    assert.equal(store.doc.timeline?.clips.length, 2)
    fireEvent.click(screen.getByRole('button', { name: '撤销' }))
    assert.equal(store.doc.timeline?.clips.length, 1)
    fireEvent.click(screen.getByRole('button', { name: '删除片段' }))
    assert.equal(store.doc.timeline?.clips.length, 0)
    assert.equal(store.doc.objects.image.src, '/images/product.png')
  } finally {
    panel.dispose()
    HTMLMediaElement.prototype.pause = originalPause
    document.body.innerHTML = ''
  }
})
