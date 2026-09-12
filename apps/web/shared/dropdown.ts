import { createMenuIcon } from './menu-icon.js'
import { nextMenuIndex } from './menu-navigation.js'
/** Shared single-select dropdown. The hidden select remains the form/state adapter. */
export function ensureDropdownStyles() {
  if (!document.getElementById('dropdown-styles')) {
    const link = document.createElement('link')
    link.id = 'dropdown-styles'
    link.rel = 'stylesheet'
    link.href = '/shared/dropdown.css?v=2'
    document.head.append(link)
  }
}

export function createDropdown(select: HTMLSelectElement) {
  ensureDropdownStyles()
  const trigger = document.createElement('button')
  trigger.type = 'button'
  trigger.className = 'ui-dropdown-trigger'
  trigger.setAttribute('role', 'combobox')
  trigger.setAttribute('aria-haspopup', 'listbox')
  trigger.setAttribute('aria-expanded', 'false')
  const text = document.createElement('span')
  const arrow = document.createElement('span')
  arrow.className = 'ui-dropdown-chevron'
  arrow.setAttribute('aria-hidden', 'true')
  const icon = createMenuIcon(select.dataset.icon || 'settings')
  if (icon) {
    trigger.append(icon)
  }
  trigger.append(text, arrow)
  const menu = document.createElement('div')
  menu.className = 'ui-dropdown-menu'
  menu.id = `dropdown-${crypto.randomUUID()}`
  menu.setAttribute('role', 'listbox')
  menu.hidden = true
  menu.setAttribute('popover', 'manual')
  trigger.setAttribute('aria-controls', menu.id)
  select.hidden = true
  select.after(trigger)
  // Keep focus inside modal ownership while popover supplies top-layer positioning.
  ;(select.closest('sl-dialog, dialog') ? select.parentElement! : document.body).append(menu)
  let buttons: HTMLButtonElement[] = []
  let active = 0

  function close(restore = false) {
    if (menu.matches(':popover-open')) {
      menu.hidePopover()
    }
    menu.hidden = true
    trigger.setAttribute('aria-expanded', 'false')
    trigger.removeAttribute('aria-activedescendant')
    if (restore) {
      trigger.focus()
    }
  }
  let signature = ''
  function sync() {
    const nextSignature = JSON.stringify([
      select.value,
      select.disabled,
      Array.from(select.options).map((o) => [o.value, o.label, o.disabled]),
    ])
    if (signature === nextSignature) {
      return
    }
    signature = nextSignature
    const selected = select.selectedOptions[0]
    text.textContent = selected?.label || '请选择'
    const label = select.getAttribute('aria-label') || '选择选项'
    trigger.setAttribute('aria-label', `${label}：${text.textContent}`)
    trigger.title = `${label}：${text.textContent}`
    menu.setAttribute('aria-label', label)
    trigger.disabled = select.disabled || select.options.length === 0
    if (!menu.hidden) {
      close()
    }
  }
  function focus(index: number) {
    active = (index + buttons.length) % buttons.length
    buttons.forEach((button, i) => {
      button.tabIndex = -1
      button.dataset.active = String(i === active)
    })
    if (buttons[active]) {
      trigger.setAttribute('aria-activedescendant', buttons[active].id)
      buttons[active].scrollIntoView({ block: 'nearest' })
    }
  }
  function open() {
    if (trigger.disabled) {
      return
    }
    document.dispatchEvent(
      new CustomEvent('ui:dropdown-open', { detail: { id: menu.id, trigger } }),
    )
    buttons = Array.from(select.options).map((option, index) => {
      const button = document.createElement('button')
      button.type = 'button'
      button.className = 'ui-dropdown-option'
      button.id = `${menu.id}-option-${index}`
      button.addEventListener('pointerdown', (event) => event.preventDefault())
      button.setAttribute('role', 'option')
      button.setAttribute('aria-selected', String(option.selected))
      const optionIcon = createMenuIcon(option.dataset.icon || select.dataset.icon || 'settings')
      if (optionIcon) {
        button.append(optionIcon)
      }
      const label = document.createElement('span')
      label.textContent = option.label
      button.append(label)
      if (option.selected) {
        const check = createMenuIcon('check')!
        check.classList.add('ui-menu-check')
        button.append(check)
      }
      button.disabled = option.disabled
      button.addEventListener('click', () => {
        select.value = option.value
        select.dispatchEvent(new Event('change', { bubbles: true }))
        sync()
        close(true)
      })
      return button
    })
    menu.replaceChildren(...buttons)
    menu.hidden = false
    menu.showPopover()
    trigger.setAttribute('aria-expanded', 'true')
    const rect = trigger.getBoundingClientRect()
    menu.style.minWidth = `${Math.min(280, Math.max(160, rect.width))}px`
    const height = menu.offsetHeight
    menu.style.left = `${Math.max(8, Math.min(innerWidth - menu.offsetWidth - 8, rect.left))}px`
    menu.style.top = `${Math.max(8, rect.top > height + 12 ? rect.top - height - 8 : Math.min(innerHeight - height - 8, rect.bottom + 8))}px`
    focus(Math.max(0, select.selectedIndex))
  }
  const click = (event: MouseEvent) => {
    event.preventDefault()
    if (menu.hidden) {
      open()
    } else {
      close()
    }
  }
  const triggerKey = (event: KeyboardEvent) => {
    if (!menu.hidden) {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault()
        buttons[active]?.click()
      } else {
        menuKey(event)
      }
      return
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      open()
    }
  }
  const menuKey = (event: KeyboardEvent) => {
    if (event.key === 'Escape') {
      event.preventDefault()
      event.stopPropagation()
      close(true)
    } else if (event.key === 'Tab') {
      close()
      trigger.focus()
    } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault()
      const next = nextMenuIndex(
        buttons.map((button) => button.disabled),
        active,
        event.key,
      )
      if (next < 0) {
        return
      }
      focus(next)
    }
  }
  const outside = (event: PointerEvent) => {
    if (
      !(event.target instanceof Node) ||
      (!trigger.contains(event.target) && !menu.contains(event.target))
    ) {
      close()
    }
  }
  const otherOpen = (event: Event) => {
    if ((event as CustomEvent<{ id: string }>).detail.id !== menu.id) {
      close()
    }
  }
  const reposition = (event: Event) => {
    // resize targets Window, which is an EventTarget but not a DOM Node.
    if (!(event.target instanceof Node) || !menu.contains(event.target)) {
      close()
    }
  }
  const observer = new MutationObserver(sync)
  observer.observe(select, { childList: true, subtree: true, attributes: true })
  trigger.addEventListener('click', click)
  trigger.addEventListener('keydown', triggerKey)
  menu.addEventListener('keydown', menuKey)
  document.addEventListener('pointerdown', outside)
  document.addEventListener('ui:dropdown-open', otherOpen)
  window.addEventListener('resize', reposition)
  window.addEventListener('scroll', reposition, true)
  const valueDescriptor = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!
  Object.defineProperty(select, 'value', {
    configurable: true,
    get() {
      return valueDescriptor.get!.call(select) as string
    },
    set(value: string) {
      valueDescriptor.set!.call(select, value)
      sync()
    },
  })
  select.addEventListener('change', sync)
  sync()
  return {
    sync,
    close,
    dispose() {
      Reflect.deleteProperty(select, 'value')
      observer.disconnect()
      trigger.remove()
      menu.remove()
      select.hidden = false
      document.removeEventListener('pointerdown', outside)
      document.removeEventListener('ui:dropdown-open', otherOpen)
      window.removeEventListener('resize', reposition)
      window.removeEventListener('scroll', reposition, true)
      select.removeEventListener('change', sync)
    },
  }
}
