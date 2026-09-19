import { timelineSrt } from '../state/subtitles.js'
import type { DocStore } from '../state/doc-store.js'

type ExportJob = {
  id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  progress: number
  error?: string
}
export function createTimelineExport(panel: HTMLElement, store: DocStore) {
  const area = document.createElement('div')
  area.className = 'timeline-export'
  area.innerHTML =
    '<button type="button">导出 MP4</button><button type="button" hidden>取消导出</button><a hidden>下载成片</a><span role="status" aria-live="polite"></span>'
  panel.querySelector('header')!.insertBefore(area, panel.querySelector('[data-action="close"]'))
  const [submit, cancel] = Array.from(area.querySelectorAll('button'))
  const download = area.querySelector('a')!
  const status = area.querySelector('span')!
  const subtitles = document.createElement('button')
  subtitles.type = 'button'
  subtitles.textContent = '下载字幕 SRT'
  subtitles.addEventListener('click', () => {
    const text = store.doc.timeline ? timelineSrt(store.doc.timeline) : ''
    if (!text) {
      status.textContent = '暂无字幕，请先填写分镜台词再装配时间线'
      return
    }
    const url = URL.createObjectURL(
      new Blob([text], { type: 'application/x-subrip;charset=utf-8' }),
    )
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = '字幕.srt'
    document.body.append(anchor)
    anchor.click()
    anchor.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    status.textContent = '字幕按当前片段时长导出；MP4 包含可开关字幕轨，显示效果取决于播放器'
  })
  area.append(subtitles)
  const key = `gencanvas.export.${store.doc.id}`
  let active = ''
  let timer: ReturnType<typeof setTimeout> | undefined
  let disposed = false
  let busy = false
  try {
    active = localStorage.getItem(key) || ''
  } catch {
    /* storage unavailable */
  }
  function remember() {
    try {
      localStorage.setItem(key, active)
    } catch {
      /* best effort */
    }
  }
  function show(job: ExportJob) {
    busy = ['queued', 'running'].includes(job.status)
    submit.disabled = busy
    cancel.hidden = !busy
    download.hidden = job.status !== 'completed'
    download.href = `/api/exports/${encodeURIComponent(job.id)}/download`
    download.textContent = '下载成片'
    download.setAttribute('download', '')
    status.textContent =
      job.status === 'completed'
        ? '导出完成（提交时的版本）'
        : job.status === 'failed' || job.status === 'cancelled'
          ? job.error || '导出未完成'
          : `导出${job.status === 'queued' ? '排队中' : ` ${job.progress}%`}`
    if (busy) {
      timer = setTimeout(() => {
        void poll()
      }, 1500)
    }
  }
  async function poll() {
    if (!active || disposed) {
      return
    }
    clearTimeout(timer)
    try {
      const res = await fetch(`/api/exports/${encodeURIComponent(active)}`)
      if (res.status === 404) {
        busy = false
        submit.disabled = false
        cancel.hidden = true
        status.textContent = '未找到该次导出，可重新导出'
        return
      }
      if (!res.ok) {
        throw new Error()
      }
      const body = (await res.json()) as { export: ExportJob }
      if (!disposed) {
        show(body.export)
      }
    } catch {
      if (!disposed) {
        status.textContent = '导出状态暂不可用，正在恢复…'
        timer = setTimeout(() => {
          void poll()
        }, 3000)
      }
    }
  }
  submit.onclick = async () => {
    if (busy) {
      return
    }
    if (!store.doc.timeline?.clips.length) {
      status.textContent = '请先加入时间线素材'
      return
    }
    busy = true
    submit.disabled = true
    download.hidden = true
    status.textContent = '正在提交导出…'
    active = crypto.randomUUID()
    remember()
    try {
      const res = await fetch('/api/exports', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          documentId: store.doc.id,
          requestId: active,
          timeline: structuredClone(store.doc.timeline),
        }),
      })
      const body = (await res.json()) as { export?: ExportJob; error?: string }
      if (!res.ok || !body.export) {
        busy = false
        submit.disabled = false
        status.textContent = body.error || '导出提交失败，请检查素材后重试'
        return
      }
      if (!disposed) {
        show(body.export)
      }
    } catch {
      if (!disposed) {
        void poll()
      }
    }
  }
  cancel.onclick = async () => {
    cancel.disabled = true
    try {
      const res = await fetch(`/api/exports/${encodeURIComponent(active)}`, { method: 'DELETE' })
      if (!res.ok) {
        throw new Error()
      }
      const body = (await res.json()) as { export: ExportJob }
      clearTimeout(timer)
      if (!disposed) {
        show(body.export)
      }
    } catch {
      status.textContent = '取消未确认，请重试'
    } finally {
      cancel.disabled = false
    }
  }
  if (active) {
    submit.disabled = true
    void poll()
  }
  return {
    dispose() {
      disposed = true
      clearTimeout(timer)
      area.remove()
    },
  }
}
