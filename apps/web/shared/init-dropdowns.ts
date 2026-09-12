import { createDropdown } from './dropdown.js'
import { createActionMenu } from './action-menu.js'

// Explicit menu selectors: ordinary details (for example FAQ accordions) are not dropdowns.
const selects = Array.from(document.querySelectorAll<HTMLSelectElement>('select')).map(
  createDropdown,
)
const menus = Array.from(
  document.querySelectorAll<HTMLDetailsElement>('.user-menu,.zoom-menu,.composer-settings'),
).map(createActionMenu)
window.addEventListener('pagehide', () => {
  selects.forEach((control) => control.close())
  menus.forEach((menu) => menu.close())
})
