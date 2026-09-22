import { timelineDuration, type Timeline } from '../state/timeline.js'
import { timelineButton, timelineTime } from './timeline-controls.js'

type Options = {
  select(id: string, frame: number): void
  seek(frame: number): void
  move(id: string, index: number): void
  add(): void
}

/** One frame-to-pixel scale drives the ruler, clips and playhead. */
export function createTimelineTrack(options: Options) {
  const element = document.createElement('div')
  element.className = 'tl-track-scroll'
  const content = document.createElement('div')
  content.className = 'tl-track-content'
  const ruler = document.createElement('div')
  ruler.className = 'tl-ruler'
  ruler.setAttribute('aria-label', '时间刻度')
  const clips = document.createElement('div')
  clips.className = 'timeline-clips'
  clips.setAttribute('aria-label', '视频轨道片段')
  const head = document.createElement('div')
  head.className = 'tl-playhead'
  content.append(ruler, clips, head)
  element.append(content)
  let scale = 30
  let total = 0
  let dragged = ''
  ruler.addEventListener('pointerdown', (event) => {
    const rect = ruler.getBoundingClientRect()
    const canvasScale = ruler.offsetWidth ? rect.width / ruler.offsetWidth : 1
    const seek = (clientX: number) =>
      options.seek(
        Math.max(
          0,
          Math.min(total - 1, Math.round(((clientX - rect.left) / (scale * canvasScale)) * 30)),
        ),
      )
    ruler.setPointerCapture(event.pointerId)
    seek(event.clientX)
    ruler.onpointermove = (move) => seek(move.clientX)
    ruler.onpointerup = ruler.onpointercancel = () => {
      ruler.onpointermove = null
    }
  })
  return {
    element,
    setFrame(frame: number) {
      head.style.left = `${(frame / 30) * scale}px`
    },
    render(timeline: Timeline, selected: string, pixelsPerSecond: number, compact = false) {
      scale = pixelsPerSecond
      total = timelineDuration(timeline)
      const seconds = Math.max(compact ? 30 : 60, Math.ceil(total / 150) * 5 + 5)
      content.style.width = `${seconds * scale}px`
      ruler.replaceChildren()
      // Keep long timelines bounded: no unbounded per-frame DOM nodes.
      const step = Math.max(5, Math.ceil(seconds / 200 / 5) * 5)
      for (let second = 0; second <= seconds; second += step) {
        const tick = document.createElement('span')
        tick.textContent = timelineTime(second * 30)
        tick.style.left = `${second * scale}px`
        ruler.append(tick)
      }
      for (const video of Array.from(clips.querySelectorAll('video'))) {
        video.pause()
        video.removeAttribute('src')
        video.load()
      }
      clips.replaceChildren()
      let position = 0
      timeline.clips.forEach((clip, index) => {
        const start = position
        const duration = clip.outFrame - clip.inFrame
        const item = timelineButton('', () => options.select(clip.id, start))
        item.className = 'timeline-clip'
        item.draggable = true
        item.setAttribute('aria-pressed', String(selected === clip.id))
        item.setAttribute('aria-label', `${clip.name}，${(duration / 30).toFixed(2)} 秒`)
        item.title = clip.name
        item.style.width = `${(duration / 30) * scale}px`
        const media = document.createElement(clip.source.kind === 'image' ? 'img' : 'video')
        media.src = clip.source.url
        if (media instanceof HTMLMediaElement) {
          media.muted = true
          media.preload = 'metadata'
          media.onloadedmetadata = () => {
            media.currentTime = clip.inFrame / 30
          }
        } else {
          media.alt = ''
          media.loading = 'lazy'
        }
        const label = document.createElement('span')
        label.textContent = timelineTime(duration)
        item.append(media, label)
        item.ondragstart = (event) => {
          dragged = clip.id
          event.dataTransfer?.setData('text/plain', clip.id)
        }
        item.ondragover = (event) => event.preventDefault()
        item.ondrop = (event) => {
          event.preventDefault()
          if (dragged) {
            options.move(dragged, index)
          }
          dragged = ''
        }
        item.ondragend = () => {
          dragged = ''
        }
        clips.append(item)
        position += duration
      })
      const add = timelineButton('＋', options.add)
      add.className = 'tl-add-clip'
      add.setAttribute('aria-label', '添加素材')
      clips.append(add)
      element.classList.toggle('tl-empty', total === 0)
    },
    dispose() {
      for (const video of Array.from(clips.querySelectorAll('video'))) {
        video.pause()
        video.removeAttribute('src')
        video.load()
      }
      element.remove()
    },
  }
}
