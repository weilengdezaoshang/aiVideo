/** Small, decorative SVGs shared by action menus and select dropdowns. */
const paths = {
  model: 'M12 3 3 8l9 5 9-5-9-5ZM3 12l9 5 9-5M3 16l9 5 9-5',
  ratio: 'M4 6h16v12H4zM8 6v12',
  resolution: 'M8 3H3v5M16 3h5v5M3 16v5h5M21 16v5h-5',
  time: 'M12 8v4l3 2M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18',
  image: 'M3 3h18v18H3zM3 16l5-5 4 4 3-3 6 6M16 7h.01',
  video: 'M3 5h12v14H3zM15 10l6-4v12l-6-4',
  text: 'M4 5h16M12 5v15M8 20h8',
  upload: 'M12 16V3M7 8l5-5 5 5M4 15v6h16v-6',
  settings: 'M4 6h16M4 12h16M4 18h16M8 3v6M16 9v6M10 15v6',
  check: 'm5 12 4 4L19 6',
} as const
export type MenuIcon = keyof typeof paths
export function createMenuIcon(name: string): SVGSVGElement | null {
  if (!Object.hasOwn(paths, name)) {
    return null
  }
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
  svg.setAttribute('viewBox', '0 0 24 24')
  svg.setAttribute('fill', 'none')
  svg.setAttribute('stroke', 'currentColor')
  svg.setAttribute('stroke-width', '1.7')
  svg.setAttribute('stroke-linecap', 'round')
  svg.setAttribute('stroke-linejoin', 'round')
  svg.setAttribute('aria-hidden', 'true')
  svg.setAttribute('focusable', 'false')
  svg.classList.add('ui-menu-icon')
  const path = document.createElementNS(svg.namespaceURI, 'path')
  path.setAttribute('d', paths[name as MenuIcon])
  svg.append(path)
  return svg
}
