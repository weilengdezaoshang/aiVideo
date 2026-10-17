import './dom.js'
import React from 'react'
import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { cleanup, fireEvent, render } from '@testing-library/react'
import { CutoutControls, type CutoutControlsProps } from '../apps/web/canvas/cutout-controls.js'

afterEach(cleanup)
function props(overrides: Partial<CutoutControlsProps> = {}): CutoutControlsProps {
  return {
    phase: 'detecting',
    error: null,
    strokeCount: 0,
    showMask: true,
    brush: { mode: 'paint', size: 22 },
    canRedo: false,
    onBrushChange() {},
    onUndo() {},
    onRedo() {},
    onToggleMask() {},
    onReset() {},
    onRetry() {},
    onApply() {},
    onCancel() {},
    ...overrides,
  }
}
test('识别开始即提供状态与取消，Escape 可退出', () => {
  let cancelled = 0
  const screen = render(<CutoutControls {...props({ onCancel: () => cancelled++ })} />)
  assert.ok(screen.getByRole('status').textContent?.includes('正在识别主体'))
  assert.equal(screen.queryByRole('button', { name: '确认抠图' }), null)
  fireEvent.keyDown(window, { key: 'Escape' })
  assert.equal(cancelled, 1)
  fireEvent.click(screen.getByRole('button', { name: '退出抠图' }))
  assert.equal(cancelled, 2)
})
test('失败保留手动修正与重试，无笔画不能提交空蒙版', () => {
  let retried = 0
  const screen = render(
    <CutoutControls {...props({ phase: 'error', error: '识别超时', onRetry: () => retried++ })} />,
  )
  assert.ok(screen.getByRole('alert').textContent?.includes('识别超时'))
  assert.equal(
    (screen.getByRole('button', { name: '确认抠图' }) as HTMLButtonElement).disabled,
    true,
  )
  fireEvent.click(screen.getByRole('alert').querySelector('button')!)
  assert.equal(retried, 1)
})
test('修正后可提交，生成时禁用退出与重复提交', () => {
  let applied = 0
  let cancelled = 0
  const screen = render(
    <CutoutControls {...props({ phase: 'error', strokeCount: 1, onApply: () => applied++ })} />,
  )
  fireEvent.click(screen.getByRole('button', { name: '确认抠图' }))
  assert.equal(applied, 1)
  screen.rerender(<CutoutControls {...props({ phase: 'applying', onCancel: () => cancelled++ })} />)
  assert.equal(
    (screen.getByRole('button', { name: '退出抠图' }) as HTMLButtonElement).disabled,
    true,
  )
  fireEvent.keyDown(window, { key: 'Escape' })
  assert.equal(cancelled, 0)
  assert.equal(screen.queryByRole('button', { name: '确认抠图' }), null)
})
test('原图对照与清空仅调用对应操作', () => {
  let toggled = 0
  let cleared = 0
  const screen = render(
    <CutoutControls
      {...props({
        phase: 'ready',
        showMask: false,
        strokeCount: 2,
        onToggleMask: () => toggled++,
        onReset: () => cleared++,
      })}
    />,
  )
  const toggle = screen.getByRole('button', { name: '对照原图' })
  assert.equal(toggle.getAttribute('aria-pressed'), 'true')
  fireEvent.click(toggle)
  fireEvent.click(screen.getByRole('button', { name: '笔刷大小' }))
  fireEvent.click(screen.getByRole('button', { name: '清空笔画' }))
  assert.equal(toggled, 1)
  assert.equal(cleared, 1)
})

test('切换橡皮擦与笔刷大小，并提供独立的笔画撤销重做', () => {
  const brushes: CutoutControlsProps['brush'][] = []
  let undo = 0
  let redo = 0
  const screen = render(
    <CutoutControls
      {...props({
        phase: 'ready',
        strokeCount: 1,
        canRedo: true,
        onBrushChange: (brush) => brushes.push(brush),
        onUndo: () => undo++,
        onRedo: () => redo++,
      })}
    />,
  )
  fireEvent.click(screen.getByRole('button', { name: '橡皮擦' }))
  assert.deepEqual(brushes[0], { mode: 'erase', size: 22 })
  fireEvent.click(screen.getByRole('button', { name: '笔刷大小' }))
  fireEvent.change(screen.getByRole('slider', { name: '笔刷大小' }), { target: { value: '48' } })
  assert.deepEqual(brushes[1], { mode: 'paint', size: 48 })
  fireEvent.click(screen.getByRole('button', { name: '撤销笔画' }))
  fireEvent.click(screen.getByRole('button', { name: '重做笔画' }))
  assert.equal(undo, 1)
  assert.equal(redo, 1)
  fireEvent.keyDown(window, { key: 'z', metaKey: true })
  fireEvent.keyDown(window, { key: 'z', metaKey: true, shiftKey: true })
  assert.equal(undo, 2)
  assert.equal(redo, 2)
  fireEvent.keyDown(window, { key: 'Escape' })
  assert.equal(screen.queryByRole('slider'), null)
})
