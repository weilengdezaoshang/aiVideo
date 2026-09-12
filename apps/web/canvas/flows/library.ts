// 素材库抽屉(PRD F10 / 线框 S17):历史网格,拖入画布或点击加入。
// 数据来自旧网格视图的 /api/history(复用,不迁移);拖拽用 HTML5 dnd,
// dataTransfer 载荷在 drop 时解析为画布对象(由 app.ts 接线落点)。

import type { CanvasEngine } from '../engine/stage.js'

const KIND_LABEL: Record<string, string> = { image: '图', video: '视频' }

export type LibraryItem = {
  id: string
  url: string
  kind: 'image' | 'video'
  width: number
  height: number
  prompt: string
}

type HistoryRecord = {
  id: string
  url: string
  params?: { kind?: string; width?: number; height?: number; prompt?: string }
}

function toItem(record: HistoryRecord): LibraryItem {
  return {
    id: record.id,
    url: record.url,
    kind: record.params?.kind === 'video' ? 'video' : 'image',
    width: record.params?.width || 512,
    height: record.params?.height || 512,
    prompt: record.params?.prompt || '',
  }
}

export type LibraryDrawer = {
  /** 画布容器接 drop:把拖拽项变成世界坐标回调 */
  attachDropTarget(engine: CanvasEngine): void
  /** 打开抽屉(供外部如素材入口) */
  open(): void
}

export function initLibrary(options: {
  getDocStore: () => unknown
  onAddToCanvas: (item: LibraryItem, world: { x: number; y: number } | null) => void
}): LibraryDrawer {
  const drawer = document.querySelector<HTMLElement>('#library-drawer')!
  const grid = document.querySelector<HTMLElement>('#library-grid')!
  const searchInput = document.querySelector<HTMLInputElement>('#library-search')!
  let items: LibraryItem[] = []
  let query = ''

  async function load() {
    try {
      const res = await fetch(
        `/api/history?limit=100${query ? `&q=${encodeURIComponent(query)}` : ''}`,
      )
      const body = (await res.json()) as { images?: HistoryRecord[] }
      items = (body.images || []).map(toItem)
      render()
    } catch {
      grid.innerHTML = '<div class="library-empty">素材加载失败,请稍后重试</div>'
    }
  }

  function render() {
    if (items.length === 0) {
      grid.innerHTML = `<div class="library-empty">${query ? '没有匹配的素材' : '暂无素材,去画布生成第一张'}</div>`
      return
    }
    grid.innerHTML = ''
    for (const item of items) {
      const cell = document.createElement('div')
      cell.className = 'library-item'
      cell.draggable = true
      cell.title = `${item.prompt}\n点击加入画布,或拖到画布任意位置`
      const img = document.createElement('img')
      img.src = item.url
      img.alt = item.prompt
      img.loading = 'lazy'
      const tag = document.createElement('span')
      tag.className = 'library-kind'
      tag.textContent = KIND_LABEL[item.kind] || '图'
      cell.append(img, tag)

      cell.addEventListener('dragstart', (e: DragEvent) => {
        e.dataTransfer?.setData('application/x-gencanvas-item', JSON.stringify(item))
        if (e.dataTransfer) {
          e.dataTransfer.effectAllowed = 'copy'
        }
      })
      cell.addEventListener('click', () => options.onAddToCanvas(item, null))
      grid.append(cell)
    }
  }

  // 搜索防抖(原生 input;历史版本用 sl-input 事件名,两者都挂)
  let searchTimer: ReturnType<typeof setTimeout> | null = null
  const onSearch = () => {
    if (searchTimer) {
      clearTimeout(searchTimer)
    }
    searchTimer = setTimeout(() => {
      query = String(searchInput.value || '').trim()
      void load()
    }, 300)
  }
  searchInput.addEventListener('input', onSearch)
  searchInput.addEventListener('sl-input', onSearch)

  document.querySelector('#library-open')!.addEventListener('click', () => {
    drawer.hidden = !drawer.hidden
    if (!drawer.hidden) {
      void load()
    }
  })
  document.querySelector('#library-close')!.addEventListener('click', () => {
    drawer.hidden = true
  })

  return {
    attachDropTarget(engine) {
      const container = engine.stage.container()
      container.addEventListener('dragover', (e: DragEvent) => {
        if (e.dataTransfer?.types.includes('application/x-gencanvas-item')) {
          e.preventDefault()
          e.dataTransfer.dropEffect = 'copy'
        }
      })
      container.addEventListener('drop', (e: DragEvent) => {
        const raw = e.dataTransfer?.getData('application/x-gencanvas-item')
        if (!raw) {
          return
        }
        e.preventDefault()
        try {
          const item = JSON.parse(raw) as LibraryItem
          const rect = container.getBoundingClientRect()
          const world = engine.screenToWorld({
            x: e.clientX - rect.left,
            y: e.clientY - rect.top,
          })
          options.onAddToCanvas(item, world)
        } catch {
          // 载荷损坏:忽略
        }
      })
    },
    /** 打开抽屉(供外部如素材入口) */
    open() {
      drawer.hidden = false
      void load()
    },
  }
}
