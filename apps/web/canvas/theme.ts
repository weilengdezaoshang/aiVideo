// 编辑器主题(PRD §4.1.1):浅色默认;显式切换仅在编辑器内记忆,
// 偏好保存与画布文档存储完全分离(localStorage);工作台/介绍页不跟随。
// 偏好无法持久化时本次选择仍生效,但不显示"偏好已保存"。

const THEME_KEY = 'gencanvas.editorTheme'

export type Theme = 'light' | 'dark'

/** @returns 当前生效主题(不含持久化是否成功) */
export function currentTheme(): Theme {
  return document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light'
}

export function applyTheme(theme: Theme) {
  const root = document.documentElement
  root.dataset.theme = theme
  // Shoelace 深色组件挂 .sl-theme-dark 命名空间
  root.classList.toggle('sl-theme-dark', theme === 'dark')
}

/** @returns 是否持久化成功 */
export function saveTheme(theme: Theme): boolean {
  try {
    localStorage.setItem(THEME_KEY, theme)
    return true
  } catch {
    return false
  }
}

/** 读取持久化偏好;无偏好返回 null(用浅色)。 */
export function loadTheme(): Theme | null {
  try {
    const value = localStorage.getItem(THEME_KEY)
    return value === 'dark' || value === 'light' ? value : null
  } catch {
    return null
  }
}

/** 接线顶栏切换按钮;返回当前主题。 */
export function initThemeToggle(button: HTMLButtonElement) {
  const sync = () => {
    const nextLabel = currentTheme() === 'dark' ? '切换到浅色主题' : '切换到深色主题'
    button.setAttribute('aria-label', nextLabel)
    button.title = nextLabel
    const label = button.querySelector('.theme-menu-label')
    if (label) {
      label.textContent = nextLabel
    }
  }
  let persisted = loadTheme()
  if (persisted) {
    applyTheme(persisted)
  }
  sync()
  button.addEventListener('click', () => {
    const next = currentTheme() === 'dark' ? 'light' : 'dark'
    applyTheme(next)
    persisted = saveTheme(next) ? next : null
    sync()
    document.dispatchEvent(new CustomEvent('gencanvas:themechange'))
  })
  return {
    get current(): Theme {
      return currentTheme()
    },
  }
}
