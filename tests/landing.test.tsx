// 落地页组件测试:模式 Tab、视频演示控件、首屏视觉参考切换、探索入口。
import './dom.js'
import test from 'node:test'
import assert from 'node:assert/strict'
import { afterEach, describe } from 'node:test'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { LandingPage } from '../apps/web/landing/LandingPage.js'

afterEach(() => cleanup())

describe('落地页', () => {
  test('首屏渲染标题与三个创作模式 Tab,默认选中 AI 生图', () => {
    render(<LandingPage />)
    assert.ok(screen.getByText('AI 图像与视频创作平台'))
    const generate = screen.getByRole('tab', { name: 'AI 生图' })
    assert.equal(generate.getAttribute('aria-selected'), 'true')
    assert.equal(
      screen.getByRole('tab', { name: '图片加工' }).getAttribute('aria-selected'),
      'false',
    )
    assert.equal(document.getElementById('mode-title')?.textContent, '把想象，变成可见。')
    assert.ok(screen.getByText('描述画面，选择模型，让第一张图从这里开始。'))
  })

  test('切换到图片加工:面板文案与示例跟随更新', () => {
    render(<LandingPage />)
    fireEvent.click(screen.getByRole('tab', { name: '图片加工' }))
    assert.equal(
      screen.getByRole('tab', { name: '图片加工' }).getAttribute('aria-selected'),
      'true',
    )
    assert.equal(
      screen.getByRole('tab', { name: 'AI 生图' }).getAttribute('aria-selected'),
      'false',
    )
    assert.equal(document.getElementById('mode-title')?.textContent, '让细节，更近一步。')
    assert.ok(screen.getByText('加工流程'))
    assert.ok(screen.getByText('选择图片 → 检查选区 → 确认加工'))
  })

  test('视频模式显示镜头运动控件与首帧参考标注', () => {
    render(<LandingPage />)
    assert.ok(document.getElementById('motion-controls')?.hidden)
    assert.ok(document.getElementById('frame-label')?.hidden)
    fireEvent.click(screen.getByRole('tab', { name: 'AI 视频' }))
    assert.equal(document.getElementById('motion-controls')?.hidden, false)
    assert.equal(document.getElementById('frame-label')?.hidden, false)
    assert.ok(screen.getByText('静态参考图的镜头运动示意 · 非 AI 生成视频'))
    assert.equal(
      document.getElementById('motion-time')?.textContent,
      '0.0 / 4.0 s',
      '初始进度输出为 0.0 / 4.0 s',
    )
  })

  test('播放按钮在播放/暂停之间切换文案与按压态', () => {
    render(<LandingPage />)
    fireEvent.click(screen.getByRole('tab', { name: 'AI 视频' }))
    const toggle = screen.getByText('播放示意')
    fireEvent.click(toggle)
    assert.ok(screen.getByText('暂停示意'))
    const pressed = document.getElementById('motion-toggle')?.getAttribute('aria-pressed')
    assert.equal(pressed, 'true')
    fireEvent.click(screen.getByText('暂停示意'))
    assert.ok(screen.getByText('播放示意'))
  })

  test('拖动进度条更新时间输出并暂停播放', () => {
    render(<LandingPage />)
    fireEvent.click(screen.getByRole('tab', { name: 'AI 视频' }))
    const input = document.getElementById('motion-progress') as HTMLInputElement
    fireEvent.input(input, { target: { value: '50' } })
    assert.equal(document.getElementById('motion-time')?.textContent, '2.0 / 4.0 s')
    assert.equal(input.value, '50')
  })

  test('「探索视频创作」切到视频模式并选中对应 Tab', () => {
    render(<LandingPage />)
    fireEvent.click(screen.getByRole('button', { name: '探索视频创作' }))
    assert.equal(screen.getByRole('tab', { name: 'AI 视频' }).getAttribute('aria-selected'), 'true')
  })

  test('首屏视觉参考按钮切换 aria-pressed 并更换图片', async () => {
    render(<LandingPage />)
    const hero = document.getElementById('hero-visual') as HTMLImageElement
    assert.equal(hero.getAttribute('src'), '/landing/assets/ocean.jpg')
    const architecture = screen.getByLabelText('切换到建筑')
    assert.equal(architecture.getAttribute('aria-pressed'), 'false')
    fireEvent.click(architecture)
    await waitFor(() => {
      assert.equal(hero.getAttribute('src'), '/landing/assets/architecture.jpg')
    })
    assert.equal(architecture.getAttribute('aria-pressed'), 'true')
    assert.equal(screen.getByLabelText('切换到海面').getAttribute('aria-pressed'), 'false')
  })

  test('Tab 键盘导航:ArrowRight 循环到下一个模式', () => {
    render(<LandingPage />)
    const generate = screen.getByRole('tab', { name: 'AI 生图' })
    fireEvent.keyDown(generate, { key: 'ArrowRight' })
    assert.equal(
      screen.getByRole('tab', { name: '图片加工' }).getAttribute('aria-selected'),
      'true',
    )
    const edit = screen.getByRole('tab', { name: '图片加工' })
    fireEvent.keyDown(edit, { key: 'End' })
    assert.equal(screen.getByRole('tab', { name: 'AI 视频' }).getAttribute('aria-selected'), 'true')
  })
})
