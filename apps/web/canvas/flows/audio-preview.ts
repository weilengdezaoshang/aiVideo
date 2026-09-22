import { clipVolume, type AudioClip } from '../state/timeline.js'

export function createAudioPreview(
  report: (message: string) => void,
  createMedia: () => HTMLAudioElement = () => document.createElement('audio'),
) {
  const players = new Map<
    string,
    { media: HTMLAudioElement; url: string; pending: boolean; failed: boolean; wanted: boolean }
  >()
  let disposed = false
  function sync(clips: AudioClip[], frame: number, playing: boolean) {
    if (disposed) {
      return
    }
    for (const [id, player] of players) {
      if (!clips.some((clip) => clip.id === id && clip.source.url === player.url)) {
        player.wanted = false
        player.media.onerror = null
        player.media.pause()
        player.media.removeAttribute('src')
        player.media.load()
        players.delete(id)
      }
    }
    for (const clip of clips) {
      let player = players.get(clip.id)
      if (!player) {
        const media = createMedia()
        media.preload = 'auto'
        media.src = clip.source.url
        player = { media, url: clip.source.url, pending: false, failed: false, wanted: false }
        players.set(clip.id, player)
        const current = player
        media.onerror = () => {
          if (!disposed && players.get(clip.id) === current) {
            current.failed = true
            current.wanted = false
            media.pause()
            report(`音轨「${clip.name}」加载失败，请检查文件是否存在或重新导入`)
          }
        }
      }
      const elapsed = frame - clip.startFrame
      const active = elapsed >= 0 && elapsed < clip.outFrame - clip.inFrame
      const media = player.media
      player.wanted = playing && active
      if (!playing || !active) {
        media.pause()
        if (!playing) {
          player.failed = false
        }
      }
      if (!active) {
        continue
      }
      const seconds = (clip.inFrame + elapsed) / 30
      if (media.readyState >= 1 && (!playing || Math.abs(media.currentTime - seconds) > 0.15)) {
        media.currentTime = seconds
      }
      media.volume = clipVolume(clip, elapsed)
      if (playing && media.readyState >= 1 && media.paused && !player.pending && !player.failed) {
        player.pending = true
        const current = player
        void media
          .play()
          .then(() => {
            if (!current.wanted || disposed || players.get(clip.id) !== current) {
              media.pause()
            }
          })
          .catch(() => {
            current.failed = true
            if (!disposed && current.wanted && players.get(clip.id) === current) {
              report(`无法播放音轨「${clip.name}」，请检查素材或重新播放`)
            }
          })
          .finally(() => {
            current.pending = false
          })
      }
    }
  }
  function pause() {
    for (const player of players.values()) {
      player.wanted = false
      player.media.pause()
    }
  }
  return {
    sync,
    pause,
    dispose() {
      disposed = true
      pause()
      for (const player of players.values()) {
        player.media.onerror = null
        player.media.removeAttribute('src')
        player.media.load()
      }
      players.clear()
    },
  }
}
