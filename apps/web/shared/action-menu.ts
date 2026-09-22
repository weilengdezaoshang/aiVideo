import { createMenuIcon } from './menu-icon.js'
import { ensureDropdownStyles } from './dropdown.js'
import { nextMenuIndex } from './menu-navigation.js'
/** Shared behavior for the canvas's existing details/summary action menus. */
export function createActionMenu(details: HTMLDetailsElement) {
  ensureDropdownStyles()
  const trigger = details.querySelector<HTMLElement>('summary')!
  const popup = trigger.nextElementSibling as HTMLElement
  popup.classList.add('ui-action-menu')
  popup.setAttribute('role', 'menu')
  trigger.setAttribute('aria-haspopup', 'menu')
  for (const item of Array.from(details.querySelectorAll<HTMLElement>('[data-menu-icon]'))) {
    const icon = createMenuIcon(item.dataset.menuIcon || '')
    if (icon) {
      item.prepend(icon)
    }
  }
  const items = () =>
    Array.from(popup.querySelectorAll<HTMLElement>('button:not(:disabled),a[href]')).filter(
      (el) => el.offsetParent !== null,
    )
  function close(restore = false) {
    details.open = false
    if (restore) {
      trigger.focus()
    }
  }
  const toggle = () => {
    trigger.setAttribute('aria-expanded', String(details.open))
    if (details.open) {
      document.dispatchEvent(
        new CustomEvent('ui:dropdown-open', { detail: { id: details.id, trigger } }),
      )
    }
  }
  const outside = (event: PointerEvent) => {
    const target = event.target as Element
    if (!details.contains(target) && !target.closest('.ui-dropdown-menu')) {
      close()
    }
  }
  const other = (event: Event) => {
    const incoming = (event as CustomEvent<{ id: string; trigger: HTMLElement }>).detail
    if (incoming.id !== details.id && !details.contains(incoming.trigger)) {
      close()
    }
  }
  const key = (event: KeyboardEvent) => {
    if (event.key === 'Escape') {
      event.preventDefault()
      event.stopPropagation()
      close(true)
    } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault()
      details.open = true
      const choices = items()
      if (!choices.length) {
        return
      }
      const current = choices.indexOf(document.activeElement as HTMLElement)
      const index = nextMenuIndex(
        choices.map(() => false),
        current,
        event.key,
      )
      choices[index]?.focus()
    }
  }
  details.addEventListener('toggle', toggle)
  details.addEventListener('keydown', key)
  document.addEventListener('pointerdown', outside)
  document.addEventListener('ui:dropdown-open', other)
  return {
    close,
    dispose() {
      details.removeEventListener('toggle', toggle)
      details.removeEventListener('keydown', key)
      document.removeEventListener('pointerdown', outside)
      document.removeEventListener('ui:dropdown-open', other)
    },
  }
}
