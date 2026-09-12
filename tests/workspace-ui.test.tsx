// 工作台 UI 基础组件冒烟:纯 React 组件在 jsdom 下渲染与交互。
import './dom.js'
import test from 'node:test'
import assert from 'node:assert/strict'
import { afterEach, describe } from 'node:test'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { Button, IconButton, SectionHeader, Tabs } from '../apps/web/ui/primitives.js'

afterEach(() => cleanup())

describe('工作台 UI 基础组件', () => {
  test('Button 渲染变体类名并触发点击', () => {
    let clicked = 0
    render(
      <Button variant="primary" onClick={() => clicked++}>
        新建画布
      </Button>,
    )
    const button = screen.getByRole('button', { name: '新建画布' })
    assert.ok(button.className.includes('ui-button--primary'))
    fireEvent.click(button)
    assert.equal(clicked, 1)
  })

  test('IconButton 带无障碍名称', () => {
    render(<IconButton label="关闭" />)
    assert.ok(screen.getByRole('button', { name: '关闭' }))
  })

  test('SectionHeader 渲染标题与徽标', () => {
    render(<SectionHeader title="我的画布" badge="12" />)
    assert.ok(screen.getByText('我的画布'))
    assert.ok(screen.getByText('12'))
  })

  test('Tabs 切换回调携带选中值,aria-pressed 同步', () => {
    let value = 'recent'
    const { rerender } = render(
      <Tabs
        label="排序方式"
        value={value}
        options={
          [
            { value: 'recent', label: '最近' },
            { value: 'name', label: '名称' },
          ] as const
        }
        onChange={(next) => {
          value = next
        }}
      />,
    )
    const recent = screen.getByRole('button', { name: '最近' })
    const nameTab = screen.getByRole('button', { name: '名称' })
    assert.equal(recent.getAttribute('aria-pressed'), 'true')
    fireEvent.click(nameTab)
    assert.equal(value, 'name')
    rerender(
      <Tabs
        label="排序方式"
        value={value}
        options={
          [
            { value: 'recent', label: '最近' },
            { value: 'name', label: '名称' },
          ] as const
        }
        onChange={(next) => {
          value = next
        }}
      />,
    )
    assert.equal(nameTab.getAttribute('aria-pressed'), 'true')
    assert.equal(recent.getAttribute('aria-pressed'), 'false')
  })
})
