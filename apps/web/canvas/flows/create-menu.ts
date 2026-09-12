import { createMenuIcon } from '../../shared/menu-icon.js'
import { cmdAddObjects } from '../state/commands.js'
import { createNode } from '../state/node-model.js'
import type { createDocStore } from '../state/doc-store.js'
import type { createCanvasEngine } from '../engine/stage.js'

type Options = {
  engine: ReturnType<typeof createCanvasEngine>
  getStore: () => ReturnType<typeof createDocStore> | null
  onCreated: (id: string, kind: string) => void
}

export function initCreateMenu({ engine, getStore, onCreated }: Options) {
  const menu = document.createElement('div')
  menu.className = 'context-menu node-create-menu'
  menu.setAttribute('role', 'menu')
  menu.setAttribute('aria-label', '新建节点')
  menu.tabIndex = -1
  menu.hidden = true
  document.body.append(menu)
  let anchor = { x: 0, y: 0 }
  const close = () => {
    menu.hidden = true
  }
  const items = [
    ['text', '文本'],
    ['image', '图片'],
    ['video', '视频'],
  ] as const
  for (const [kind, title] of items) {
    const button = document.createElement('button')
    button.type = 'button'
    button.className = 'context-menu-item'
    button.setAttribute('role', 'menuitem')
    button.append(createMenuIcon(kind)!, document.createTextNode(title))
    button.addEventListener('click', () => {
      const store = getStore()
      if (!store || !engine.interaction.canEdit()) {
        close()
        return
      }
      const count = Object.values(store.doc.objects).filter((obj) => obj.kind === kind).length
      const obj = createNode(kind, anchor, `${title} ${count + 1}`)
      store.apply(cmdAddObjects([obj]))
      close()
      onCreated(obj.id, kind)
    })
    menu.append(button)
  }
  window.addEventListener('pointerdown', (e) => {
    if (!menu.contains(e.target as Node)) {
      close()
    }
  })
  window.addEventListener('resize', close)
  engine.stage.container().addEventListener('wheel', close)
  engine.interaction.subscribe((state) => {
    if (state.gesture || state.effective !== 'select') {
      close()
    }
  })
  menu.addEventListener('keydown', (e) => {
    const buttons = Array.from(menu.querySelectorAll('button'))
    const index = buttons.indexOf(document.activeElement as HTMLButtonElement)
    if (e.key === 'Escape' || e.key === 'Tab') {
      close()
    }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      const next =
        index < 0
          ? e.key === 'ArrowDown'
            ? 0
            : buttons.length - 1
          : (index + (e.key === 'ArrowDown' ? 1 : buttons.length - 1)) % buttons.length
      buttons[next].focus()
    }
  })
  return {
    open(x: number, y: number, world: { x: number; y: number }) {
      if (!getStore() || !engine.interaction.canEdit()) {
        return
      }
      anchor = world
      menu.hidden = false
      const rect = menu.getBoundingClientRect()
      menu.style.left = `${Math.max(8, Math.min(x, window.innerWidth - rect.width - 8))}px`
      menu.style.top = `${Math.max(8, Math.min(y, window.innerHeight - rect.height - 8))}px`
      menu.focus({ preventScroll: true })
    },
  }
}
