export type TimelineClip = {
  id: string
  name: string
  subtitle?: string
  volume?: number
  fadeInFrames?: number
  fadeOutFrames?: number
  source: {
    nodeId: string
    kind: 'image' | 'video'
    url: string
    durationFrames: number
    /** 分镜服务端执行装配标识：用于判断本轮是否已装配 */
    runId?: string
    shotId?: string
  }
  inFrame: number
  outFrame: number
}
export type AudioClip = {
  id: string
  name: string
  source: { assetId: string; url: string; durationFrames: number }
  startFrame: number
  inFrame: number
  outFrame: number
  volume: number
  fadeInFrames: number
  fadeOutFrames: number
}
export type Timeline = {
  /** Position of the document's embedded editor in world coordinates. */
  canvas?: { x: number; y: number }
  version: 1
  fps: 30
  width: number
  height: number
  clips: TimelineClip[]
  audioClips?: AudioClip[]
}
export function emptyTimeline(): Timeline {
  return { version: 1, fps: 30, width: 1920, height: 1080, clips: [] }
}
export function timelineDuration(timeline: Timeline): number {
  return timeline.clips.reduce((n, c) => n + c.outFrame - c.inFrame, 0)
}
export function clipAtFrame(timeline: Timeline, frame: number) {
  let start = 0
  for (const clip of timeline.clips) {
    const end = start + clip.outFrame - clip.inFrame
    if (frame >= start && frame < end) {
      return { clip, start, sourceFrame: clip.inFrame + frame - start }
    }
    start = end
  }
  return null
}
export function trimClip(
  timeline: Timeline,
  id: string,
  inFrame: number,
  outFrame: number,
): Timeline {
  const clip = timeline.clips.find((item) => item.id === id)
  if (
    !clip ||
    !Number.isInteger(inFrame) ||
    !Number.isInteger(outFrame) ||
    inFrame < 0 ||
    outFrame <= inFrame ||
    outFrame > clip.source.durationFrames
  ) {
    throw new Error('入点和出点必须在素材时长内，且至少保留一帧')
  }
  return {
    ...timeline,
    clips: timeline.clips.map((item) => (item.id === id ? { ...item, inFrame, outFrame } : item)),
  }
}
export function splitClip(timeline: Timeline, frame: number, newId: string): Timeline {
  const hit = clipAtFrame(timeline, frame)
  if (!Number.isInteger(frame) || !hit || frame === hit.start) {
    throw new Error('请把播放头移到片段内部再分割')
  }
  if (timeline.clips.some((clip) => clip.id === newId)) {
    throw new Error('片段标识重复')
  }
  return {
    ...timeline,
    clips: timeline.clips.flatMap((clip) =>
      clip.id === hit.clip.id
        ? [
            { ...clip, outFrame: hit.sourceFrame },
            { ...clip, id: newId, inFrame: hit.sourceFrame },
          ]
        : [clip],
    ),
  }
}
export function moveClip(timeline: Timeline, id: string, index: number): Timeline {
  const clips = [...timeline.clips]
  const from = clips.findIndex((clip) => clip.id === id)
  if (from < 0 || !Number.isInteger(index) || index < 0 || index >= clips.length) {
    throw new Error('片段位置无效')
  }
  const [clip] = clips.splice(from, 1)
  clips.splice(index, 0, clip)
  return { ...timeline, clips }
}

export function clipVolume(
  clip: Pick<TimelineClip, 'inFrame' | 'outFrame' | 'volume' | 'fadeInFrames' | 'fadeOutFrames'>,
  elapsed: number,
): number {
  const duration = clip.outFrame - clip.inFrame
  const fadeIn = Math.min(clip.fadeInFrames || 0, duration)
  const fadeOut = Math.min(clip.fadeOutFrames || 0, duration)
  return (
    (clip.volume ?? 1) *
    (fadeIn ? Math.max(0, Math.min(1, elapsed / fadeIn)) : 1) *
    (fadeOut ? Math.max(0, Math.min(1, (duration - elapsed) / fadeOut)) : 1)
  )
}
