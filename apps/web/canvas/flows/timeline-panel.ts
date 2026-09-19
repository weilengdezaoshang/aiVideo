import { createAudioPreview } from './audio-preview.js'
import { createAudioTracks } from './audio-tracks.js'
import { videoDuration } from './media-duration.js'
import { createTimelineExport } from './timeline-export.js'
import type { DocStore } from '../state/doc-store.js'
import {
  clipVolume,
  emptyTimeline,
  timelineDuration,
  clipAtFrame,
  trimClip,
  splitClip,
  moveClip,
  type Timeline,
  type TimelineClip,
} from '../state/timeline.js'
import { objectOriginalUrl } from './asset-util.js'
import type { CanvasEngine } from '../engine/stage.js'
import { cmdAddObjects, type CanvasObj } from '../state/commands.js'
import { createTimelineTrack } from '../components/timeline-track.js'
import { createTimelineAssets } from '../components/timeline-assets.js'
import { createTimelineCanvas } from '../components/timeline-canvas.js'
import { timelineTime } from '../components/timeline-controls.js'

export function createTimelinePanel(
  store: DocStore,
  getSelection: () => string[],
  engine?: CanvasEngine,
) {
  const link = document.createElement('link')
  link.rel = 'stylesheet'
  link.href = '/canvas/timeline.css'
  document.head.append(link)
  const workspaceStyle = document.createElement('link')
  workspaceStyle.rel = 'stylesheet'
  workspaceStyle.href = '/canvas/timeline-workspace.css'
  document.head.append(workspaceStyle)
  const toggle = document.createElement('button')
  toggle.type = 'button'
  toggle.className = 'timeline-toggle'
  toggle.textContent = '时间线'
  toggle.setAttribute('aria-expanded', 'false')
  toggle.setAttribute('aria-controls', 'timeline-panel')
  document.querySelector('.topbar-tools')!.prepend(toggle)
  const panel = document.createElement('section')
  panel.id = 'timeline-panel'
  panel.className = 'timeline-panel'
  panel.setAttribute('aria-label', '时间线编辑器')
  panel.hidden = true
  panel.innerHTML = `
    <header><strong>时间线</strong><button type="button" data-action="add">加入选中素材</button><button type="button" data-action="undo">撤销</button><button type="button" data-action="redo">重做</button><label>画幅<select aria-label="成片画幅"><option value="1920x1080">16:9 横屏</option><option value="1080x1920">9:16 竖屏</option><option value="1080x1080">1:1 方形</option></select></label><span class="timeline-status" role="status" aria-live="polite"></span><button type="button" data-action="close">收起</button></header>
    <div class="timeline-body"><div class="timeline-preview"><img alt="时间线预览" hidden><video playsinline preload="metadata" hidden></video><span>加入图片或视频开始剪辑</span></div>
    <div class="timeline-editor"><div class="timeline-transport"><button type="button" data-action="play">播放</button><label>播放位置<input class="timeline-seek" type="range" min="0" max="0" value="0" step="1" aria-label="播放位置"></label><output class="timeline-time"></output><label>缩放<input class="timeline-zoom" type="range" min="20" max="100" value="40" aria-label="时间线缩放"></label></div>
    <div class="timeline-scroll"><div class="timeline-clips" role="list" aria-label="视频轨道片段"></div></div>
    <div class="timeline-properties"><label>入点（秒）<input class="timeline-in" type="number" min="0" step="0.033333" aria-label="片段入点（秒）"></label><label>出点（秒）<input class="timeline-out" type="number" min="0" step="0.033333" aria-label="片段出点（秒）"></label><button type="button" data-action="trim">应用裁剪</button><button type="button" data-action="split">在播放头分割</button><button type="button" data-action="earlier">前移</button><button type="button" data-action="later">后移</button><button type="button" data-action="duplicate">复制</button><button type="button" data-action="remove">删除片段</button></div></div></div>`
  document.body.append(panel)
  panel.setAttribute('role', 'dialog')
  panel.setAttribute('aria-modal', 'true')
  const trackOptions = {
    select(id: string, position: number) {
      stop()
      selected = id
      frame = position
      render()
    },
    seek(position: number) {
      stop()
      frame = position
      renderPreview()
    },
    move(id: string, index: number) {
      commit(moveClip(timeline(), id, index))
    },
    add() {
      setOpen(true)
      assets.element.querySelector<HTMLButtonElement>('button')?.focus()
    },
  }
  const track = createTimelineTrack(trackOptions)
  const compactTrack = createTimelineTrack(trackOptions)
  panel.querySelector('.timeline-scroll')!.replaceWith(track.element)
  const assets = createTimelineAssets({
    getObjects: () => Object.values(store.doc.objects),
    getAdded: () => new Set(timeline().clips.map((clip) => clip.source.nodeId)),
    add: (ids) => addSelection(ids),
    importFiles,
  })
  panel.querySelector('.timeline-body')!.prepend(assets.element)
  const embedded = engine
    ? createTimelineCanvas({
        engine,
        store,
        track: compactTrack,
        play: () => get<HTMLButtonElement>('[data-action="play"]').click(),
        expand: () => setOpen(true),
        export: () => {
          setOpen(true)
          get<HTMLButtonElement>('.timeline-export button').click()
        },
        add: () => {
          if (getSelection().length) {
            void addSelection()
          } else {
            setOpen(true)
          }
        },
        move: (canvas) => commit({ ...timeline(), canvas }),
      })
    : undefined
  const exporter = createTimelineExport(panel, store)
  const audioTracks = createAudioTracks(panel, store)
  const get = <T extends HTMLElement>(selector: string) => panel.querySelector<T>(selector)!
  const status = get('.timeline-status')
  const seek = get<HTMLInputElement>('.timeline-seek')
  const zoom = get<HTMLInputElement>('.timeline-zoom')
  const image = get<HTMLImageElement>('img')
  const video = get<HTMLVideoElement>('video')
  const caption = document.createElement('div')
  caption.className = 'timeline-caption'
  caption.setAttribute('aria-label', '当前片段字幕')
  get('.timeline-preview').append(caption)
  const subtitle = document.createElement('textarea')
  subtitle.className = 'timeline-subtitle'
  subtitle.setAttribute('aria-label', '片段字幕')
  subtitle.placeholder = '当前片段字幕（整段显示）'
  subtitle.maxLength = 4000
  get('.timeline-properties').append(subtitle)
  subtitle.addEventListener('change', () => {
    const tl = timeline()
    if (tl.clips.some((clip) => clip.id === selected)) {
      commit({
        ...tl,
        clips: tl.clips.map((clip) =>
          clip.id === selected ? { ...clip, subtitle: subtitle.value } : clip,
        ),
      })
    }
  })
  const audioFields = new Map<string, HTMLInputElement>()
  for (const [field, title, max] of [
    ['volume', '原声音量（%）', 100],
    ['fadeInFrames', '淡入（秒）', 3600],
    ['fadeOutFrames', '淡出（秒）', 3600],
  ] as const) {
    const label = document.createElement('label')
    label.textContent = title
    const input = document.createElement('input')
    input.type = 'number'
    input.min = '0'
    input.max = String(max)
    input.step = field === 'volume' ? '1' : '0.1'
    input.setAttribute('aria-label', title)
    label.append(input)
    get('.timeline-properties').append(label)
    audioFields.set(field, input)
    input.addEventListener('change', () => {
      const value = Number(input.value)
      if (!Number.isFinite(value) || value < 0 || value > max) {
        render()
        return
      }
      const stored = field === 'volume' ? value / 100 : Math.round(value * 30)
      commit({
        ...timeline(),
        clips: timeline().clips.map((clip) =>
          clip.id === selected ? { ...clip, [field]: stored } : clip,
        ),
      })
    })
  }
  const previewHint = get('.timeline-preview span')
  let selected = ''
  let frame = 0
  let playing = false
  let animation = 0
  let startedAt = 0
  let startedFrame = 0
  let previewId = ''
  let adding = false
  let disposed = false
  const timeline = () => store.doc.timeline || emptyTimeline()
  const message = (text: string) => {
    status.textContent = text
  }
  const audioPreview = createAudioPreview(message)
  // Keep the compact editing actions out of the full-screen title bar.
  const toolbar = get('.timeline-transport')
  for (const action of ['add', 'undo', 'redo']) {
    toolbar.prepend(get(`[data-action="${action}"]`))
  }
  toolbar.append(get('select').closest('label')!)
  const previewControls = document.createElement('div')
  previewControls.className = 'tl-preview-controls'
  previewControls.append(get('[data-action="play"]'), get('output'))
  get('.timeline-preview').append(previewControls)
  const properties = document.createElement('details')
  properties.className = 'tl-properties-disclosure'
  const summary = document.createElement('summary')
  summary.textContent = '片段属性 · 时长 / 字幕 / 原声'
  properties.append(summary, get('.timeline-properties'))
  get('.timeline-editor').append(properties)
  function stop() {
    audioPreview.pause()
    playing = false
    cancelAnimationFrame(animation)
    video.pause()
    get('[data-action="play"]').textContent = '播放'
    if (store.doc.timeline) {
      embedded?.update(frame, false)
    }
  }
  function commit(next: Timeline) {
    stop()
    store.apply({
      type: 'setTimeline',
      before: store.doc.timeline ? structuredClone(store.doc.timeline) : undefined,
      after: next,
    })
    message('')
  }
  function renderPreview() {
    const tl = timeline()
    const total = timelineDuration(tl)
    frame = Math.max(0, Math.min(frame, Math.max(0, total - 1)))
    seek.max = String(Math.max(0, total - 1))
    seek.value = String(frame)
    seek.disabled = total === 0
    get('output').textContent = `${timelineTime(frame)} / ${timelineTime(total)}`
    track.setFrame(frame)
    if (store.doc.timeline) {
      embedded?.update(frame, playing)
    }
    audioPreview.sync(tl.audioClips || [], frame, playing)
    const hit = clipAtFrame(tl, frame)
    get('.timeline-preview').style.aspectRatio = `${tl.width}/${tl.height}`
    caption.textContent = hit?.clip.subtitle || ''
    caption.hidden = !caption.textContent
    if (!hit) {
      video.pause()
      video.hidden = image.hidden = true
      previewHint.hidden = false
      previewId = ''
      return
    }
    const { clip, sourceFrame } = hit
    const key = `${clip.id}:${clip.source.url}`
    const changed = previewId !== key
    if (changed) {
      video.pause()
      previewId = key
      image.hidden = clip.source.kind !== 'image'
      video.hidden = clip.source.kind !== 'video'
      previewHint.hidden = true
      if (clip.source.kind === 'image') {
        image.src = clip.source.url
      } else {
        video.src = clip.source.url
      }
    }
    if (clip.source.kind === 'video') {
      video.volume = clipVolume(clip, frame - hit.start)
      const seconds = sourceFrame / 30
      if (
        video.readyState >= 1 &&
        (changed || !playing || Math.abs(video.currentTime - seconds) > 0.2)
      ) {
        video.currentTime = seconds
      }
      if (playing && video.paused) {
        void video.play().catch(() => {
          stop()
          message('视频无法播放，请检查素材或重新加入')
        })
      }
    }
  }
  video.addEventListener('loadedmetadata', renderPreview)
  for (const media of [image, video]) {
    media.addEventListener('error', () => {
      stop()
      message('素材无法读取，请检查原文件或重新加入')
    })
  }
  function render() {
    const tl = timeline()
    embedded?.refresh()
    if (store.doc.timeline) {
      embedded?.update(frame, playing)
    }
    if (!tl.clips.some((clip) => clip.id === selected)) {
      selected =
        clipAtFrame(tl, Math.min(frame, Math.max(0, timelineDuration(tl) - 1)))?.clip.id ||
        tl.clips[0]?.id ||
        ''
    }
    compactTrack.render(tl, selected, 22, true)
    const chosen = tl.clips.find((clip) => clip.id === selected)
    const sizeSelect = get<HTMLSelectElement>('select')
    const sizeValue = `${tl.width}x${tl.height}`
    if (!Array.from(sizeSelect.options).some((option) => option.value === sizeValue)) {
      sizeSelect.add(new Option(`${tl.width} × ${tl.height}`, sizeValue))
    }
    sizeSelect.value = sizeValue
    const input = get<HTMLInputElement>('.timeline-in')
    const output = get<HTMLInputElement>('.timeline-out')
    input.value = chosen ? String(chosen.inFrame / 30) : ''
    output.value = chosen ? String(chosen.outFrame / 30) : ''
    for (const [field, control] of audioFields) {
      control.disabled = !chosen || chosen.source.kind !== 'video'
      const value = chosen?.[field as 'volume' | 'fadeInFrames' | 'fadeOutFrames']
      control.value = String(field === 'volume' ? (value ?? 1) * 100 : (value || 0) / 30)
    }
    input.disabled = output.disabled = !chosen
    subtitle.disabled = !chosen
    if (document.activeElement !== subtitle) {
      subtitle.value = chosen?.subtitle || ''
    }
    for (const name of ['trim', 'split', 'earlier', 'later', 'duplicate', 'remove']) {
      get<HTMLButtonElement>(`[data-action="${name}"]`).disabled = !chosen
    }
    get<HTMLButtonElement>('[data-action="undo"]').disabled = !store.canUndo()
    get<HTMLButtonElement>('[data-action="redo"]').disabled = !store.canRedo()
    get<HTMLButtonElement>('[data-action="play"]').disabled = !tl.clips.length
    track.render(tl, selected, Number(zoom.value))
    assets.render()
    renderPreview()
  }
  async function addSelection(ids = getSelection()) {
    if (adding) {
      return
    }
    const objects = ids
      .map((id) => store.doc.objects[id])
      .filter((obj) => obj && ['image', 'video'].includes(obj.kind) && (obj.src || obj.assetId))
    if (!objects.length) {
      message('请先选中已生成或上传的图片、视频')
      return
    }
    adding = true
    get<HTMLButtonElement>('[data-action="add"]').disabled = true
    message('正在读取素材时长…')
    try {
      const clips: TimelineClip[] = []
      for (const obj of objects) {
        const url = objectOriginalUrl(obj)
        const durationFrames = obj.kind === 'video' ? await videoDuration(url) : 108000
        if (disposed || store.doc.objects[obj.id] !== obj || objectOriginalUrl(obj) !== url) {
          throw new Error('素材已变更，请重新选择并加入')
        }
        clips.push({
          id: crypto.randomUUID(),
          name: (obj.name || (obj.kind === 'video' ? '视频' : '图片')).slice(0, 500),
          source: {
            nodeId: obj.id,
            kind: obj.kind === 'video' ? 'video' : 'image',
            url,
            durationFrames,
          },
          inFrame: 0,
          outFrame: obj.kind === 'video' ? durationFrames : 150,
        })
      }
      selected = clips[0].id
      frame = timelineDuration(timeline())
      commit({ ...timeline(), clips: [...timeline().clips, ...clips] })
    } catch (error) {
      message(error instanceof Error ? error.message : '素材读取失败')
    } finally {
      adding = false
      get<HTMLButtonElement>('[data-action="add"]').disabled = false
    }
  }
  async function importFiles(files: File[]) {
    for (const file of files) {
      try {
        if (disposed) {
          return
        }
        const formats: Record<string, string> = {
          'image/png': 'png',
          'image/jpeg': 'jpg',
          'image/webp': 'webp',
          'video/mp4': 'mp4',
          'video/webm': 'webm',
        }
        const ext = formats[file.type]
        if (!ext || file.size > 40 * 1024 * 1024) {
          throw new Error('请选择 40MB 以内的 PNG、JPEG、WebP、MP4 或 WebM 文件')
        }
        message(`正在导入 ${file.name}`)
        const kind = file.type.startsWith('video/') ? 'video' : 'image'
        const response = await fetch(
          `/api/assets?ext=${ext}&kind=${kind}&name=${encodeURIComponent(file.name)}`,
          {
            method: 'POST',
            headers: { 'Content-Type': file.type },
            body: file,
          },
        )
        const body = (await response.json()) as {
          asset?: { id: string; ext: string; width?: number; height?: number }
          error?: string
        }
        if (!response.ok || !body.asset) {
          throw new Error(body.error || '素材导入失败')
        }
        if (disposed) {
          return
        }
        const position = timeline().canvas || { x: 0, y: 0 }
        const obj: CanvasObj = {
          id: `timeline-import-${crypto.randomUUID()}`,
          kind,
          name: file.name,
          assetId: body.asset.id,
          ext: body.asset.ext,
          hasThumbs: false,
          x: position.x - 360,
          y: position.y - 240,
          width: 280,
          height:
            body.asset.width && body.asset.height
              ? (280 * body.asset.height) / body.asset.width
              : 157.5,
        }
        store.apply(cmdAddObjects([obj]))
        await addSelection([obj.id])
      } catch (error) {
        message(error instanceof Error ? error.message : '导入失败')
      }
    }
  }
  function setOpen(open: boolean) {
    stop()
    panel.hidden = !open
    toggle.setAttribute('aria-expanded', String(open))
    document.body.classList.toggle('timeline-open', open)
    if (open) {
      render()
      get<HTMLButtonElement>('[data-action="close"]').focus()
    } else {
      toggle.focus()
    }
  }
  toggle.onclick = () => {
    if (engine && !store.doc.timeline?.canvas) {
      const canvas = engine.screenToWorld({ x: 200, y: 250 })
      commit({ ...timeline(), canvas })
      engine.centerOn({ x: canvas.x + 360, y: canvas.y + 90 }, 1)
    } else {
      setOpen(panel.hidden)
    }
  }
  get('select').addEventListener('change', () => {
    const [width, height] = get<HTMLSelectElement>('select').value.split('x').map(Number)
    commit({ ...timeline(), width, height })
  })
  seek.oninput = () => {
    stop()
    frame = Number(seek.value)
    renderPreview()
  }
  zoom.oninput = render
  function tick(now: number) {
    if (!playing) {
      return
    }
    frame = startedFrame + Math.floor(((now - startedAt) * 30) / 1000)
    if (frame >= timelineDuration(timeline())) {
      stop()
      renderPreview()
      return
    }
    renderPreview()
    animation = requestAnimationFrame(tick)
  }
  panel.addEventListener('click', (event) => {
    const action = (event.target as HTMLElement).closest<HTMLButtonElement>('button[data-action]')
      ?.dataset.action
    if (!action) {
      return
    }
    const tl = timeline()
    const index = tl.clips.findIndex((c) => c.id === selected)
    try {
      if (action === 'close') {
        setOpen(false)
      } else if (action === 'add') {
        void addSelection()
      } else if (action === 'undo') {
        stop()
        store.undo()
      } else if (action === 'redo') {
        stop()
        store.redo()
      } else if (action === 'trim') {
        commit(
          trimClip(
            tl,
            selected,
            Math.round(Number(get<HTMLInputElement>('.timeline-in').value) * 30),
            Math.round(Number(get<HTMLInputElement>('.timeline-out').value) * 30),
          ),
        )
      } else if (action === 'split') {
        commit(splitClip(tl, frame, crypto.randomUUID()))
      } else if (action === 'remove') {
        commit({ ...tl, clips: tl.clips.filter((c) => c.id !== selected) })
      } else if (action === 'earlier' || action === 'later') {
        commit(
          moveClip(
            tl,
            selected,
            Math.max(0, Math.min(tl.clips.length - 1, index + (action === 'earlier' ? -1 : 1))),
          ),
        )
      } else if (action === 'duplicate' && index >= 0) {
        const clips = [...tl.clips]
        clips.splice(index + 1, 0, { ...structuredClone(clips[index]), id: crypto.randomUUID() })
        commit({ ...tl, clips })
      } else if (action === 'play') {
        if (playing) {
          stop()
        } else {
          if (frame >= timelineDuration(tl) - 1) {
            frame = 0
          }
          playing = true
          startedFrame = frame
          startedAt = performance.now()
          get('[data-action="play"]').textContent = '暂停'
          animation = requestAnimationFrame(tick)
        }
      }
    } catch (error) {
      message(error instanceof Error ? error.message : '编辑失败')
    }
  })
  // 时间线操作不向画布快捷键传播，避免 Delete 误删画布对象。
  panel.addEventListener('keydown', (event) => {
    event.stopPropagation()
    if (event.key === 'Escape') {
      setOpen(false)
    }
    if (event.key === 'Tab') {
      const controls = Array.from(
        panel.querySelectorAll<HTMLElement>(
          'button:not(:disabled),input:not(:disabled),select,textarea,a[href]',
        ),
      ).filter((el) => !el.hidden && el.getClientRects().length)
      const first = controls[0],
        last = controls.at(-1)
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last?.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first?.focus()
      }
    }
  })
  const unsubscribe = store.subscribe(() => {
    stop()
    render()
  })
  render()
  return {
    refresh: () => embedded?.refresh(),
    dispose() {
      embedded?.dispose()
      compactTrack.dispose()
      track.dispose()
      exporter.dispose()
      audioTracks.dispose()
      audioPreview.dispose()
      disposed = true
      stop()
      unsubscribe()
      panel.remove()
      toggle.remove()
      link.remove()
      workspaceStyle.remove()
      document.body.classList.remove('timeline-open')
    },
  }
}
