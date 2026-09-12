// 编辑器主题:偏好持久化与文档存储分离(PRD §4.1.1)。
import './dom.js'
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  applyTheme,
  currentTheme,
  initThemeToggle,
  loadTheme,
  saveTheme,
} from '../apps/web/canvas/theme.js'

test('applyTheme 切换 data-theme 与 Shoelace 深色命名空间', () => {
  const root = document.documentElement
  applyTheme('dark')
  assert.equal(root.dataset.theme, 'dark')
  assert.ok(root.classList.contains('sl-theme-dark'))
  applyTheme('light')
  assert.equal(root.dataset.theme, 'light')
  assert.equal(root.classList.contains('sl-theme-dark'), false)
  assert.equal(currentTheme(), 'light')
})

test('saveTheme/loadTheme 往返;loadTheme 拒绝非法值', () => {
  assert.equal(saveTheme('dark'), true)
  assert.equal(loadTheme(), 'dark')
  localStorage.setItem('gencanvas.editorTheme', 'blue')
  assert.equal(loadTheme(), null)
})

test('initThemeToggle:点击切换主题、持久化并派发事件', () => {
  localStorage.removeItem('gencanvas.editorTheme')
  const button = document.createElement('button')
  const label = document.createElement('span')
  label.className = 'theme-menu-label'
  button.append(label)
  document.body.append(button)
  let events = 0
  document.addEventListener('gencanvas:themechange', () => events++)
  try {
    const handle = initThemeToggle(button)
    assert.equal(handle.current, 'light')
    button.click()
    assert.equal(handle.current, 'dark')
    assert.equal(loadTheme(), 'dark', '切换后的偏好写入本机存储')
    assert.ok(button.title.includes('浅色'), '按钮标签指向可切换到的目标主题')
    assert.equal(events, 1)
    button.click()
    assert.equal(handle.current, 'light')
    assert.equal(events, 2)
  } finally {
    button.remove()
    localStorage.removeItem('gencanvas.editorTheme')
  }
})
