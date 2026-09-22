import type { DocStore } from '../state/doc-store.js'
import { emptyTimeline, timelineDuration, type AudioClip } from '../state/timeline.js'

export function createAudioTracks(panel: HTMLElement, store: DocStore) {
  const section = document.createElement('details')
  const summary = document.createElement('summary')
  summary.textContent = '旁白与音乐音轨'
  section.append(summary)
  const upload = document.createElement('input')
  upload.type = 'file'
  upload.accept = '.wav,audio/wav'
  upload.setAttribute('aria-label', '导入 WAV 音频')
  const status = document.createElement('span')
  status.setAttribute('role', 'status')
  const warnings = document.createElement('p')
  warnings.setAttribute('role', 'status')
  section.append(warnings)
  const list = document.createElement('div')
  section.append(upload, status, list)
  panel.append(section)
  const controller = new AbortController()
  const timeline = () => store.doc.timeline || emptyTimeline()
  const save = (audioClips: AudioClip[]) =>
    store.apply({
      type: 'setTimeline',
      before: store.doc.timeline,
      after: { ...timeline(), audioClips },
    })
  upload.addEventListener('change', () => {
    const file = upload.files?.[0]
    if (!file) {
      return
    }
    if (file.size > 40 * 1024 * 1024) {
      status.textContent = '音频不能超过 40 MB'
      return
    }
    upload.disabled = true
    status.textContent = '正在导入…'
    void fetch(`/api/assets?kind=audio&ext=wav&name=${encodeURIComponent(file.name)}`, {
      method: 'POST',
      body: file,
      signal: controller.signal,
    })
      .then(async (response) => {
        const result = (await response.json()) as {
          asset?: { id: string; durationSec: number }
          error?: string
        }
        if (!response.ok || !result.asset) {
          throw new Error(result.error || '导入失败')
        }
        if (controller.signal.aborted) {
          return
        }
        if ((timeline().audioClips?.length || 0) >= 64) {
          throw new Error('音轨数量达到上限')
        }
        const asset = result.asset
        const frames = Math.floor(asset.durationSec * 30)
        if (!Number.isFinite(frames) || frames < 1 || frames > 108000) {
          throw new Error('音频时长无效')
        }
        save([
          ...(timeline().audioClips || []),
          {
            id: crypto.randomUUID(),
            name: file.name,
            source: {
              assetId: asset.id,
              url: `/assets/${asset.id}/original.wav`,
              durationFrames: frames,
            },
            startFrame: 0,
            inFrame: 0,
            outFrame: frames,
            volume: 1,
            fadeInFrames: 0,
            fadeOutFrames: 0,
          },
        ])
        status.textContent = '已导入；成片长度由视频轨决定'
      })
      .catch((error: unknown) => {
        status.textContent = error instanceof Error ? error.message : '导入失败'
      })
      .finally(() => {
        upload.disabled = false
        upload.value = ''
      })
  })
  function render() {
    const end = timelineDuration(timeline())
    const outside = (timeline().audioClips || []).filter(
      (clip) => clip.startFrame + clip.outFrame - clip.inFrame > end,
    )
    warnings.textContent = outside.length
      ? `以下音轨超出画面结尾，超出部分不会预览或导出：${outside.map((clip) => clip.name).join('、')}。请延长画面或裁剪音频。`
      : ''
    list.replaceChildren()
    for (const clip of timeline().audioClips || []) {
      const row = document.createElement('div')
      row.className = 'timeline-properties'
      const name = document.createElement('strong')
      name.textContent = clip.name
      row.append(name)
      for (const [field, title] of [
        ['startFrame', '起点'],
        ['inFrame', '入点'],
        ['outFrame', '出点'],
        ['volume', '音量'],
        ['fadeInFrames', '淡入'],
        ['fadeOutFrames', '淡出'],
      ] as const) {
        const input = document.createElement('input')
        input.type = 'number'
        input.min = '0'
        input.step = '0.1'
        input.value = String(field === 'volume' ? clip[field] * 100 : clip[field] / 30)
        const label = document.createElement('label')
        label.textContent = `${title}（${field === 'volume' ? '%' : '秒'}）`
        input.setAttribute('aria-label', `${clip.name} ${title}`)
        label.append(input)
        row.append(label)
        input.addEventListener('change', () => {
          const value = Number(input.value)
          const next = {
            ...clip,
            [field]: field === 'volume' ? value / 100 : Math.round(value * 30),
          }
          if (
            !Number.isFinite(value) ||
            value < 0 ||
            next.volume > 1 ||
            next.inFrame >= next.outFrame ||
            next.outFrame > clip.source.durationFrames ||
            next.startFrame + next.outFrame - next.inFrame > 108000 ||
            next.fadeInFrames > 108000 ||
            next.fadeOutFrames > 108000
          ) {
            status.textContent = '音轨参数超出范围'
            render()
            return
          }
          save((timeline().audioClips || []).map((item) => (item.id === clip.id ? next : item)))
        })
      }
      const remove = document.createElement('button')
      remove.textContent = '删除音轨'
      remove.type = 'button'
      remove.addEventListener('click', () =>
        save((timeline().audioClips || []).filter((item) => item.id !== clip.id)),
      )
      row.append(remove)
      list.append(row)
    }
  }
  let previous = ''
  const unsubscribe = store.subscribe(() => {
    const next = JSON.stringify([timeline().audioClips, timelineDuration(timeline())])
    if (previous !== next) {
      previous = next
      render()
    }
  })
  render()
  return {
    dispose() {
      controller.abort()
      unsubscribe()
      section.remove()
    },
  }
}
