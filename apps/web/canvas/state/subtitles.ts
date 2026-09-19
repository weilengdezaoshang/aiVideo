import type { Timeline } from './timeline.js'

function stamp(frame: number, fps: number): string {
  const ms = Math.round((frame * 1000) / fps)
  const pad = (n: number, width = 2) => String(n).padStart(width, '0')
  return `${pad(Math.floor(ms / 3600000))}:${pad(Math.floor(ms / 60000) % 60)}:${pad(Math.floor(ms / 1000) % 60)},${pad(ms % 1000, 3)}`
}

/** 每片段一句字幕，按当前剪辑时长排布；后续可扩展句内对齐。 */
export function timelineSrt(timeline: Timeline): string {
  let frame = 0
  const cues: string[] = []
  for (const clip of timeline.clips) {
    const end = frame + clip.outFrame - clip.inFrame
    const text = (clip.subtitle || '')
      .replace(/\r\n?/g, '\n')
      .replace(/\n\s*\n/g, '\n')
      .trim()
    if (text) {
      cues.push(
        `${cues.length + 1}\n${stamp(frame, timeline.fps)} --> ${stamp(end, timeline.fps)}\n${text}`,
      )
    }
    frame = end
  }
  return cues.length ? cues.join('\n\n') + '\n' : ''
}
