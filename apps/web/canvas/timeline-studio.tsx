import { useEffect, useRef, useState } from 'react'
import type { DocStore } from './state/doc-store.js'
import {
  clipAtFrame,
  clipVolume,
  emptyTimeline,
  moveClip,
  splitClip,
  timelineDuration,
  trimClip,
  type Timeline,
} from './state/timeline.js'
import { createTimelineTrack } from './components/timeline-track.js'
import { createTimelineExport } from './flows/timeline-export.js'
import { createAudioTracks } from './flows/audio-tracks.js'
import { createAudioPreview } from './flows/audio-preview.js'
import { mediaSource } from './state/studio-bindings.js'
import type { CanvasObj } from './state/commands.js'

export function TimelineStudio({
  store,
  media,
  active,
  ensureSaved,
  exportKey,
}: {
  store: DocStore
  media: CanvasObj[]
  active: boolean
  ensureSaved: () => Promise<unknown>
  exportKey: string
}) {
  const timeline = store.doc.timeline ?? emptyTimeline()
  const [frame, setFrame] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [selected, setSelected] = useState('')
  const [notice, setNotice] = useState('')
  const trackHost = useRef<HTMLDivElement>(null)
  const servicesHost = useRef<HTMLDivElement>(null)
  const videoRef = useRef<HTMLVideoElement>(null)
  const track = useRef<ReturnType<typeof createTimelineTrack> | null>(null)
  const audio = useRef<ReturnType<typeof createAudioPreview> | null>(null)
  const current = useRef(timeline)
  current.current = timeline
  const total = timelineDuration(timeline)
  const hit = clipAtFrame(timeline, frame)
  const apply = (next: Timeline) =>
    store.apply({ type: 'setTimeline', before: store.doc.timeline, after: next })
  const attempt = (operation: () => void) => {
    try {
      operation()
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '编辑失败')
    }
  }
  useEffect(() => {
    const host = trackHost.current
    const services = servicesHost.current
    if (!host || !services) {
      return
    }
    const next = createTimelineTrack({
      select: (id, at) => {
        setSelected(id)
        setFrame(at)
        setPlaying(false)
      },
      seek: (at) => {
        setFrame(at)
        setPlaying(false)
      },
      move: (id, index) =>
        store.apply({
          type: 'setTimeline',
          before: store.doc.timeline,
          after: moveClip(current.current, id, index),
        }),
      add: () => setNotice('从下方选择画布素材加入时间线'),
    })
    track.current = next
    host.append(next.element)
    const exports = createTimelineExport(services, store, { ensureSaved, storageKey: exportKey })
    const tracks = createAudioTracks(services, store)
    audio.current = createAudioPreview(setNotice)
    return () => {
      next.element.remove()
      track.current = null
      exports.dispose()
      tracks.dispose()
      audio.current?.dispose()
      audio.current = null
    }
  }, [store, ensureSaved, exportKey])
  useEffect(() => {
    track.current?.render(timeline, selected, 45, true)
  }, [timeline, selected])
  useEffect(() => {
    track.current?.setFrame(frame)
  }, [frame])
  useEffect(() => {
    if (!active) {
      setPlaying(false)
    }
  }, [active])
  useEffect(() => {
    if (!playing || !active || total < 1) {
      return
    }
    const started = performance.now()
    const initial = frame
    let token = 0
    const tick = (now: number) => {
      const next = initial + Math.floor(((now - started) * 30) / 1000)
      setFrame(Math.min(total - 1, next))
      if (next >= total - 1) {
        setPlaying(false)
        return
      }
      token = requestAnimationFrame(tick)
    }
    token = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(token)
    // Playback starts at the current cursor, then advances from one monotonic clock.
  }, [playing, active, total])
  useEffect(() => {
    if (total === 0) {
      setPlaying(false)
      setFrame(0)
    }
    if (frame >= total && total > 0) {
      setFrame(total - 1)
    }
    audio.current?.sync(timeline.audioClips ?? [], frame, playing && active && total > 0)
    const video = videoRef.current
    if (!video || !hit) {
      return
    }
    const seconds = hit.sourceFrame / 30
    if (video.readyState >= 1 && (!playing || Math.abs(video.currentTime - seconds) > 0.15)) {
      video.currentTime = seconds
    }
    video.volume = clipVolume(hit.clip, frame - hit.start)
    if (playing && active) {
      void video.play().catch(() => {
        setNotice('视频无法播放，请检查素材')
        setPlaying(false)
      })
    } else {
      video.pause()
    }
  }, [timeline, frame, playing, active, hit?.clip.id, total])
  const add = (object: CanvasObj) => {
    const url = mediaSource(object)
    if (!url || (object.kind !== 'image' && object.kind !== 'video')) {
      return
    }
    const durationFrames =
      object.kind === 'image' ? 90 : Math.max(1, Math.round((object.durationSec ?? 4) * 30))
    apply({
      ...timeline,
      clips: [
        ...timeline.clips,
        {
          id: crypto.randomUUID(),
          name: object.name || '素材',
          source: { nodeId: object.id, kind: object.kind, url, durationFrames },
          inFrame: 0,
          outFrame: durationFrames,
        },
      ],
    })
  }
  return (
    <div className="next-panel-body timeline-studio">
      <h2>时间线</h2>
      <div className="timeline-preview">
        {hit?.clip.source.kind === 'image' ? (
          <img src={hit.clip.source.url} alt={hit.clip.name} />
        ) : hit ? (
          <video
            key={hit.clip.id}
            ref={videoRef}
            src={hit.clip.source.url}
            playsInline
            preload="metadata"
            onLoadedMetadata={() => {
              if (videoRef.current) {
                videoRef.current.currentTime = hit.sourceFrame / 30
              }
            }}
            onError={() => {
              setNotice('视频素材加载失败')
              setPlaying(false)
            }}
          />
        ) : (
          <p>选择画布素材加入时间线</p>
        )}
        {hit?.clip.subtitle && <span className="timeline-subtitle">{hit.clip.subtitle}</span>}
      </div>
      <div className="next-row-actions">
        <button
          disabled={!total}
          onClick={() => {
            if (frame >= total - 1) {
              setFrame(0)
            }
            setPlaying(!playing)
          }}
        >
          {playing ? '暂停' : '播放'}
        </button>
        <output>
          {(frame / 30).toFixed(2)} / {(total / 30).toFixed(2)} 秒
        </output>
        <button
          disabled={!total}
          onClick={() => attempt(() => apply(splitClip(timeline, frame, crypto.randomUUID())))}
        >
          在播放头分割
        </button>
      </div>
      <input
        type="range"
        aria-label="时间线播放头"
        min="0"
        max={Math.max(0, total - 1)}
        value={Math.min(frame, Math.max(0, total - 1))}
        onChange={(event) => {
          setPlaying(false)
          setFrame(Number(event.target.value))
        }}
      />
      <div ref={trackHost} />
      <details>
        <summary>添加画布素材</summary>
        {media
          .filter(
            (object) => (object.kind === 'image' || object.kind === 'video') && mediaSource(object),
          )
          .map((object) => (
            <button key={object.id} onClick={() => add(object)}>
              添加 {object.name || object.kind}
            </button>
          ))}
      </details>
      {timeline.clips.map((clip, index) => (
        <section className="next-shot-card" key={clip.id}>
          <strong>{clip.name}</strong>
          <label>
            字幕
            <textarea
              aria-label={`${clip.name}字幕`}
              value={clip.subtitle ?? ''}
              onChange={(event) =>
                apply({
                  ...timeline,
                  clips: timeline.clips.map((item) =>
                    item.id === clip.id ? { ...item, subtitle: event.target.value } : item,
                  ),
                })
              }
            />
          </label>
          <label>
            入点（帧）
            <input
              type="number"
              min="0"
              max={clip.outFrame - 1}
              value={clip.inFrame}
              onChange={(event) =>
                attempt(() =>
                  apply(trimClip(timeline, clip.id, Number(event.target.value), clip.outFrame)),
                )
              }
            />
          </label>
          <label>
            出点（帧）
            <input
              type="number"
              min={clip.inFrame + 1}
              max={clip.source.durationFrames}
              value={clip.outFrame}
              onChange={(event) =>
                attempt(() =>
                  apply(trimClip(timeline, clip.id, clip.inFrame, Number(event.target.value))),
                )
              }
            />
          </label>
          <label>
            音量
            <input
              type="range"
              min="0"
              max="1"
              step="0.05"
              value={clip.volume ?? 1}
              onChange={(event) =>
                apply({
                  ...timeline,
                  clips: timeline.clips.map((item) =>
                    item.id === clip.id ? { ...item, volume: Number(event.target.value) } : item,
                  ),
                })
              }
            />
          </label>
          <button
            disabled={index === 0}
            onClick={() => apply(moveClip(timeline, clip.id, index - 1))}
          >
            前移
          </button>
          <button
            disabled={index === timeline.clips.length - 1}
            onClick={() => apply(moveClip(timeline, clip.id, index + 1))}
          >
            后移
          </button>
          <button
            onClick={() =>
              apply({ ...timeline, clips: timeline.clips.filter((item) => item.id !== clip.id) })
            }
          >
            删除片段
          </button>
        </section>
      ))}
      <div ref={servicesHost}>
        <header>
          <span>成片导出</span>
          <button data-action="close" hidden />
        </header>
      </div>
      <p role="status">{notice}</p>
    </div>
  )
}
