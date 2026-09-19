/** 画布视频悬停预览:静音循环播放,支持进度条拖动逐帧预览,离开后停在首帧. */

export type HoverVideoMedia = {
  muted: boolean
  loop: boolean
  playsInline: boolean
  currentTime: number
  duration: number
  readyState: number
  play(): Promise<void>
  pause(): void
  addEventListener?(type: 'seeked', listener: () => void): void
  removeEventListener?(type: 'seeked', listener: () => void): void
}

export type VideoHoverPreview = {
  start(id: string, media: HoverVideoMedia): void
  /** 停止指定对象;省略 id 时停止当前预览. */
  stop(id?: string): void
  dispose(): void
  activeId(): string | null
  /** 拖动进度条前暂停当前预览,不回封面帧. */
  beginScrub(): void
  /** 拖动中更新播放位置,仅在 beginScrub 之后生效. */
  scrubTo(seconds: number): void
  /** 松手后从拖动位置恢复播放. */
  endScrub(): void
  isScrubbing(): boolean
  progress(): { current: number; duration: number; ratio: number } | null
}

/** 拖动目标秒数钳制到 [0, duration];时长未就绪时只能停在 0. */
export function clampSeek(seconds: number, duration: number): number {
  if (!Number.isFinite(seconds) || seconds <= 0) {
    return 0
  }
  if (!Number.isFinite(duration) || duration <= 0) {
    return 0
  }
  return Math.min(seconds, duration)
}

/** 播放进度比例;时长或当前位置无效时按 0 处理,避免 NaN 宽度. */
export function hoverPlayProgress(current: number, duration: number): number {
  if (!Number.isFinite(duration) || duration <= 0) {
    return 0
  }
  if (!Number.isFinite(current) || current <= 0) {
    return 0
  }
  return Math.min(current / duration, 1)
}

/** 用启动时记下的请求 URL 判断源是否变化,不读浏览器规范化后的 media.src. */
export function shouldStopHoverForSourceChange(input: {
  hovering: boolean
  activeHoverUrl: string | null
  requestedUrl: string | null
}): boolean {
  if (!input.hovering) {
    return false
  }
  if (!input.requestedUrl) {
    return true
  }
  return input.activeHoverUrl !== null && input.activeHoverUrl !== input.requestedUrl
}

/** 仅精细指针悬停启动预览;触摸点击仍走选择/拖动/双击播放. */
export function shouldStartVideoHover(input: {
  pointerType?: string | null
  hoverFine?: boolean
}): boolean {
  if (input.pointerType && input.pointerType !== 'mouse') {
    return false
  }
  if (input.hoverFine === false) {
    return false
  }
  return true
}

export function hoverFinePointer(
  media: (query: string) => { matches: boolean } = (query) => window.matchMedia(query),
): boolean {
  return media('(hover: hover)').matches && media('(pointer: fine)').matches
}

export type PosterRecacheMedia = {
  addEventListener?(type: 'seeked', listener: () => void): void
  removeEventListener?(type: 'seeked', listener: () => void): void
}

/**
 * 停预览后 currentTime=0 的 seek 可能异步完成,不能立刻把未到位的最后一帧烘焙进去.
 * 返回取消函数,避免 seeked 迟到时改写已重悬停/已销毁/已换源的组.
 */
export function schedulePosterRecache(
  media: PosterRecacheMedia,
  recache: () => void,
  isStale: () => boolean,
  seekRequested: boolean,
): () => void {
  let cancelled = false
  const finish = () => {
    if (cancelled || isStale()) {
      return
    }
    recache()
  }
  if (!seekRequested || typeof media.addEventListener !== 'function') {
    finish()
    return () => {
      cancelled = true
    }
  }
  const onSeeked = () => {
    media.removeEventListener?.('seeked', onSeeked)
    finish()
  }
  media.addEventListener('seeked', onSeeked)
  return () => {
    cancelled = true
    media.removeEventListener?.('seeked', onSeeked)
  }
}

export function createVideoHoverPreview(
  options: {
    onPlaying?: (id: string) => void
    onPaused?: (id: string) => void
    onStopped?: (id: string, media: HoverVideoMedia, meta: { seekRequested: boolean }) => void
  } = {},
): VideoHoverPreview {
  let disposed = false
  let generation = 0
  let activeId: string | null = null
  let activeMedia: HoverVideoMedia | null = null
  let scrubbing = false

  function restorePoster(media: HoverVideoMedia): boolean {
    const from = media.currentTime
    media.pause()
    try {
      media.currentTime = 0
    } catch {
      /* 元数据未就绪时 seek 可能失败 */
      return false
    }
    return from !== 0
  }

  function stopActive() {
    const id = activeId
    const media = activeMedia
    scrubbing = false
    generation += 1
    activeId = null
    activeMedia = null
    if (!id || !media) {
      return
    }
    const seekRequested = restorePoster(media)
    options.onStopped?.(id, media, { seekRequested })
  }

  return {
    start(id, media) {
      if (disposed) {
        return
      }
      if (activeId === id && activeMedia === media) {
        return
      }
      stopActive()
      const token = generation + 1
      generation = token
      activeId = id
      activeMedia = media
      scrubbing = false
      media.muted = true
      media.loop = true
      media.playsInline = true
      options.onPlaying?.(id)
      let playing: Promise<void>
      try {
        playing = Promise.resolve(media.play())
      } catch {
        playing = Promise.reject(new Error('play failed'))
      }
      void playing
        .then(() => {
          // 同一元素可能被新一代会话(重入或共享 URL)继续使用,不得 pause 当前预览.
          if (activeMedia === media) {
            return
          }
          restorePoster(media)
        })
        .catch(() => {
          /* 自动播放被拒或 pause 打断 play;迟到的拒绝不得重启预览.拖动中的 pause 打断属于主动行为,交由 endScrub 重新播放. */
          if (generation === token && activeId === id && activeMedia === media && !scrubbing) {
            const seekRequested = restorePoster(media)
            activeId = null
            activeMedia = null
            generation += 1
            options.onStopped?.(id, media, { seekRequested })
          }
        })
    },
    stop(id) {
      if (id && activeId !== id) {
        return
      }
      stopActive()
    },
    dispose() {
      disposed = true
      stopActive()
    },
    activeId: () => activeId,
    beginScrub() {
      if (disposed || !activeMedia || !activeId) {
        return
      }
      scrubbing = true
      activeMedia.pause()
      options.onPaused?.(activeId)
    },
    scrubTo(seconds) {
      if (disposed || !activeMedia || !scrubbing) {
        return
      }
      activeMedia.currentTime = clampSeek(seconds, activeMedia.duration)
    },
    endScrub() {
      if (disposed || !activeMedia || !activeId) {
        scrubbing = false
        return
      }
      scrubbing = false
      const id = activeId
      const media = activeMedia
      const token = generation
      options.onPlaying?.(id)
      let playing: Promise<void>
      try {
        playing = Promise.resolve(media.play())
      } catch {
        playing = Promise.reject(new Error('play failed'))
      }
      void playing.catch(() => {
        /* 拖动后恢复播放被拒时回封面,不得留下既不播放也不回封面的僵尸态. */
        if (generation === token && activeId === id && activeMedia === media) {
          const seekRequested = restorePoster(media)
          activeId = null
          activeMedia = null
          generation += 1
          options.onStopped?.(id, media, { seekRequested })
        }
      })
    },
    isScrubbing: () => scrubbing,
    progress() {
      if (!activeMedia) {
        return null
      }
      const duration = activeMedia.duration
      const current = activeMedia.currentTime
      return { current, duration, ratio: hoverPlayProgress(current, duration) }
    },
  }
}
