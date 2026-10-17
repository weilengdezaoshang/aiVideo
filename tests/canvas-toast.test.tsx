import './dom.js'
import React from 'react'
import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { CanvasToast, useCanvasNotice } from '../apps/web/canvas/canvas-toast.js'

afterEach(cleanup)
function Harness() {
  const [notice, setNotice] = useCanvasNotice()
  return (
    <>
      <button onClick={() => setNotice('抠图已完成，已添加到画布')}>成功</button>
      <button onClick={() => setNotice('另一条提示')}>其他提示</button>
      <CanvasToast notice={notice} />
    </>
  )
}

test('成功提示满3秒开始退出，过渡结束后移除', (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const screen = render(<Harness />)
  fireEvent.click(screen.getByText('成功'))
  act(() => t.mock.timers.tick(2999))
  assert.equal(screen.getByRole('status').getAttribute('data-visible'), 'true')
  act(() => t.mock.timers.tick(1))
  assert.equal(screen.container.querySelector('.next-toast')?.getAttribute('data-visible'), 'false')
  act(() => t.mock.timers.tick(180))
  assert.equal(screen.container.querySelector('.next-toast'), null)
})

test('相同提示重复触发重新计时，旧计时器不能关闭新提示', (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const screen = render(<Harness />)
  fireEvent.click(screen.getByText('成功'))
  act(() => t.mock.timers.tick(2500))
  fireEvent.click(screen.getByText('成功'))
  act(() => t.mock.timers.tick(1000))
  assert.equal(screen.getByRole('status').getAttribute('data-visible'), 'true')
  act(() => t.mock.timers.tick(2000))
  assert.equal(screen.container.querySelector('.next-toast')?.getAttribute('data-visible'), 'false')
})

test('退场期间的新提示恢复显示，卸载清理计时器', (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const screen = render(<Harness />)
  fireEvent.click(screen.getByText('成功'))
  act(() => t.mock.timers.tick(3000))
  fireEvent.click(screen.getByText('其他提示'))
  act(() => t.mock.timers.tick(180))
  assert.equal(screen.getByRole('status').textContent, '另一条提示')
  assert.equal(screen.getByRole('status').getAttribute('data-visible'), 'true')
  screen.unmount()
  act(() => t.mock.timers.tick(4000))
  assert.equal(screen.container.querySelector('.next-toast'), null)
})
