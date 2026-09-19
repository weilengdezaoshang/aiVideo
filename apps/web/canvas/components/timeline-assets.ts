import type { CanvasObj } from '../state/commands.js'
import { objectOriginalUrl } from '../flows/asset-util.js'
import { timelineButton } from './timeline-controls.js'

export function createTimelineAssets(options: {
  getObjects(): CanvasObj[]
  getAdded(): Set<string>
  add(ids: string[]): Promise<void>
  importFiles(files: File[]): Promise<void>
}) {
  const element = document.createElement('aside')
  element.className = 'tl-assets'
  element.setAttribute('aria-label', '时间线素材库')
  const tabs = document.createElement('div')
  tabs.className = 'tl-asset-tabs'
  let source = 'canvas'
  let filter = 'all'
  const imported = timelineButton('已导入资产', () => {
    source = 'imported'
    render()
  })
  const canvas = timelineButton('画布资产', () => {
    source = 'canvas'
    render()
  })
  tabs.append(imported, canvas)
  const filters = document.createElement('div')
  filters.className = 'tl-asset-filters'
  for (const [value, label] of [
    ['all', '全部'],
    ['image', '图片'],
    ['video', '视频'],
  ]) {
    const button = timelineButton(label, () => {
      filter = value
      render()
    })
    button.dataset.filter = value
    filters.append(button)
  }
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = 'image/png,image/jpeg,image/webp,video/mp4,video/webm'
  input.multiple = true
  input.hidden = true
  input.onchange = async () => {
    const files = Array.from(input.files || [])
    input.value = ''
    await options.importFiles(files)
    source = 'imported'
    render()
  }
  filters.append(timelineButton('＋ 导入', () => input.click()))
  const grid = document.createElement('div')
  grid.className = 'tl-asset-grid'
  element.append(tabs, filters, input, grid)
  function render() {
    imported.setAttribute('aria-pressed', String(source === 'imported'))
    canvas.setAttribute('aria-pressed', String(source === 'canvas'))
    for (const button of Array.from(filters.querySelectorAll<HTMLButtonElement>('[data-filter]'))) {
      button.setAttribute('aria-pressed', String(button.dataset.filter === filter))
    }
    grid.replaceChildren()
    const objects = options
      .getObjects()
      .filter(
        (obj) =>
          ['image', 'video'].includes(obj.kind) &&
          (obj.src || obj.assetId) &&
          (source === 'canvas' || obj.id.startsWith('timeline-import-')) &&
          (filter === 'all' || obj.kind === filter),
      )
    const added = options.getAdded()
    for (const obj of objects) {
      const card = timelineButton('', async () => {
        card.disabled = true
        try {
          await options.add([obj.id])
        } finally {
          card.disabled = false
        }
      })
      card.className = 'tl-asset-card'
      card.setAttribute('aria-label', `添加 ${obj.name || '素材'}`)
      const media = document.createElement(obj.kind === 'video' ? 'video' : 'img')
      media.src = objectOriginalUrl(obj)
      if (media instanceof HTMLMediaElement) {
        media.muted = true
        media.preload = 'metadata'
      } else {
        media.alt = ''
        media.loading = 'lazy'
      }
      const label = document.createElement('span')
      label.textContent = obj.name || (obj.kind === 'video' ? '视频' : '图片')
      card.append(media, label)
      if (added.has(obj.id)) {
        const badge = document.createElement('small')
        badge.textContent = '已添加'
        card.append(badge)
      }
      grid.append(card)
    }
    if (!objects.length) {
      grid.textContent =
        source === 'imported' ? '导入图片或视频开始剪辑' : '画布中暂无已完成的图片或视频'
    }
  }
  return { element, render }
}
