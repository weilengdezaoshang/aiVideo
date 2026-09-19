import type { CanvasEngine } from '../engine/stage.js'
import type { DocStore } from '../state/doc-store.js'
import { timelineDuration } from '../state/timeline.js'
import { timelineButton, timelineTime } from './timeline-controls.js'
import { createTimelineTrack } from './timeline-track.js'
import { transformPoint } from '../engine/transform-geometry.js'

export function createTimelineCanvas(options: {
  engine: CanvasEngine
  store: DocStore
  track: ReturnType<typeof createTimelineTrack>
  play(): void
  expand(): void
  export(): void
  add(): void
  move(position: { x: number; y: number }): void
}) {
  const { engine, store } = options
  const element = document.createElement('section')
  element.className = 'tl-canvas-node'
  element.setAttribute('aria-label', '画布时间线')
  const title = document.createElement('div')
  title.className = 'tl-node-title'
  title.textContent = '时间线 1 · 拖动移动'
  const header = document.createElement('header')
  const play = timelineButton('播放', options.play)
  const time = document.createElement('output')
  header.append(
    play,
    time,
    timelineButton('导出', options.export),
    timelineButton('全屏编辑', options.expand),
  )
  const port = timelineButton('＋', options.add)
  port.className = 'tl-input-port'
  port.setAttribute('aria-label', '添加选中素材到时间线')
  element.append(title, port, header, options.track.element)
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
  svg.classList.add('tl-canvas-connections')
  svg.setAttribute('aria-hidden', 'true')
  const overlay = document.createElement('div')
  overlay.className = 'tl-canvas-overlay'
  overlay.append(svg, element)
  document.body.append(overlay)
  for (const name of ['pointerdown', 'click', 'dblclick', 'keydown']) {
    element.addEventListener(name, (event) => event.stopPropagation())
  }
  let dragging: { clientX: number; clientY: number; x: number; y: number } | undefined
  let transient: { x: number; y: number } | undefined
  let disposed = false
  let refreshQueued = false
  function scheduleRefresh() {
    if (disposed || refreshQueued) {
      return
    }
    refreshQueued = true
    queueMicrotask(() => {
      refreshQueued = false
      if (!disposed) {
        refresh()
      }
    })
  }
  title.onpointerdown = (event) => {
    if (event.button !== 0) {
      return
    }
    const position = store.doc.timeline?.canvas
    if (!position) {
      return
    }
    title.setPointerCapture(event.pointerId)
    dragging = { clientX: event.clientX, clientY: event.clientY, ...position }
  }
  title.onpointermove = (event) => {
    if (!dragging) {
      return
    }
    const scale = engine.getCam().scale
    transient = {
      x: dragging.x + (event.clientX - dragging.clientX) / scale,
      y: dragging.y + (event.clientY - dragging.clientY) / scale,
    }
    refresh()
  }
  title.onpointerup = () => {
    const next = transient
    dragging = transient = undefined
    if (next) {
      options.move(next)
    }
  }
  title.onpointercancel = () => {
    dragging = transient = undefined
    refresh()
  }
  function refresh() {
    const tl = store.doc.timeline
    const position = transient || tl?.canvas
    element.hidden = !position
    svg.style.display = position ? '' : 'none'
    if (!position || !tl) {
      return
    }
    const rect = engine.stage.container().getBoundingClientRect() as DOMRect
    const point = engine.worldToScreen(position)
    const scale = engine.getCam().scale
    const x = point.x + rect.left
    const y = point.y + rect.top
    element.style.transform = `translate(${x}px, ${y}px) scale(${scale})`
    // Clip overlays to the canvas surface; never cover app toolbars.
    const clip = `inset(${rect.top}px ${Math.max(0, window.innerWidth - rect.right)}px ${Math.max(0, window.innerHeight - rect.bottom)}px ${rect.left}px)`
    overlay.style.clipPath = clip
    svg.replaceChildren()
    for (const id of new Set(tl.clips.map((clip) => clip.source.nodeId))) {
      const obj = store.doc.objects[id]
      if (!obj) {
        continue
      }
      // During a drag (including multi-selection), positions live in Konva until
      // dragend commits one undoable command. Do not read the stale document position.
      const node = engine.contentLayer
        .getChildren()
        .find((child: { id(): string }) => child.id() === id)
      const geometry = node ? { ...obj, x: node.x(), y: node.y(), rotation: node.rotation() } : obj
      const start = engine.worldToScreen(transformPoint(geometry, obj.width, obj.height / 2))
      const sx = start.x + rect.left,
        sy = start.y + rect.top
      const endY = y + 75 * scale
      const reach = Math.max(60 * scale, Math.abs(x - sx) / 2)
      const path = document.createElementNS(svg.namespaceURI, 'path')
      path.setAttribute(
        'd',
        `M ${sx} ${sy} C ${sx + reach} ${sy}, ${x - reach} ${endY}, ${x} ${endY}`,
      )
      svg.append(path)
    }
  }
  const sizeOff = engine.onSizeChanged(refresh)
  engine.stage.on(
    'dragmove.timelineCanvas dragend.timelineCanvas transform.timelineCanvas',
    scheduleRefresh,
  )
  return {
    refresh,
    update(frame: number, playing: boolean) {
      play.textContent = playing ? '暂停' : '播放'
      play.disabled = !store.doc.timeline?.clips.length
      time.textContent = `${timelineTime(frame)} / ${timelineTime(timelineDuration(store.doc.timeline!))}`
      options.track.setFrame(frame)
    },
    dispose() {
      disposed = true
      engine.stage.off('.timelineCanvas')
      sizeOff()
      element.remove()
      svg.remove()
      overlay.remove()
    },
  }
}
