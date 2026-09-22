import type { DocStore } from '../state/doc-store.js'
import { emptyTimeline, type Timeline, type TimelineClip } from '../state/timeline.js'
import type { Shot } from '../state/storyboard.js'
import { objectOriginalUrl } from './asset-util.js'
import { videoDuration } from './media-duration.js'

type SourceInfo = { id: string; kind: 'image' | 'video'; url: string }

function resolveSources(
  shots: Shot[],
  store: DocStore,
  offset: number,
): { shots: Shot[]; sources: SourceInfo[] } {
  const sources = shots.map((shot, index) => {
    const node = shot.nodeId ? store.doc.objects[shot.nodeId] : undefined
    if (!node || !['image', 'video'].includes(node.kind) || !objectOriginalUrl(node)) {
      throw new Error(`镜头 ${offset + index + 1}「${shot.title}」缺少已生成素材`)
    }
    return { id: node.id, kind: node.kind as 'image' | 'video', url: objectOriginalUrl(node) }
  })
  return { shots, sources }
}

async function buildClips(
  shots: Shot[],
  sources: SourceInfo[],
  duration: (url: string) => Promise<number>,
  signal: AbortSignal | undefined,
  markers?: { runId?: string },
): Promise<TimelineClip[]> {
  const clips: TimelineClip[] = []
  for (const [index, shot] of shots.entries()) {
    if (signal?.aborted) {
      throw new Error('已取消装配')
    }
    const source = sources[index]
    const durationFrames = source.kind === 'video' ? await duration(source.url) : 108000
    if (shot.durationFrames > durationFrames) {
      throw new Error(
        `镜头 ${index + 1}「${shot.title}」视频不足 ${(shot.durationFrames / 30).toFixed(1)} 秒，请缩短分镜或更换素材`,
      )
    }
    clips.push({
      id: crypto.randomUUID(),
      name: shot.title,
      subtitle: shot.dialogue,
      source: {
        nodeId: source.id,
        kind: source.kind,
        url: source.url,
        durationFrames,
        ...markers,
        shotId: shot.id,
      },
      inFrame: 0,
      outFrame: shot.durationFrames,
    })
  }
  return clips
}

function commitAppend(
  store: DocStore,
  previous: Timeline | undefined,
  board: unknown,
  sources: SourceInfo[],
  clips: TimelineClip[],
  signal: AbortSignal | undefined,
) {
  if (signal?.aborted || signalAbortedOrChanged(store, previous, board, sources)) {
    throw new Error('分镜、素材或时间线已变化，请重新装配')
  }
  const timeline = previous || emptyTimeline()
  if (timeline.clips.length + clips.length > 2000) {
    throw new Error('时间线片段数量超过上限')
  }
  store.apply({
    type: 'setTimeline',
    before: previous,
    after: { ...timeline, clips: [...timeline.clips, ...clips] },
  })
}

function signalAbortedOrChanged(
  store: DocStore,
  previous: Timeline | undefined,
  board: unknown,
  sources: SourceInfo[],
): boolean {
  return (
    JSON.stringify(store.doc.storyboard) !== JSON.stringify(board) ||
    JSON.stringify(store.doc.timeline) !== JSON.stringify(previous) ||
    sources.some((source) => {
      const current = store.doc.objects[source.id]
      return !current || current.kind !== source.kind || objectOriginalUrl(current) !== source.url
    })
  )
}

export async function appendStoryboard(
  store: DocStore,
  duration = videoDuration,
  signal?: AbortSignal,
) {
  const board = structuredClone(store.doc.storyboard)
  const previous = structuredClone(store.doc.timeline)
  if (!board?.shots.length) {
    throw new Error('请先添加分镜')
  }
  const { shots, sources } = resolveSources(board.shots, store, 0)
  const clips = await buildClips(shots, sources, duration, signal)
  commitAppend(store, previous, board, sources, clips, signal)
}

/** 按执行记录的镜头顺序装配，片段带执行标识；同一执行只追加一次。 */
export async function appendRunStoryboard(
  store: DocStore,
  runId: string,
  shotIds: string[],
  duration = videoDuration,
  signal?: AbortSignal,
) {
  const board = structuredClone(store.doc.storyboard)
  const previous = structuredClone(store.doc.timeline)
  if (!board?.shots.length) {
    throw new Error('请先添加分镜')
  }
  if (previous?.clips.some((clip) => clip.source.runId === runId)) {
    throw new Error('本轮结果已装配过时间线')
  }
  const shots = shotIds.map((id, index) => {
    const shot = board.shots.find((item) => item.id === id)
    if (!shot) {
      throw new Error(`镜头 ${index + 1} 已不在当前分镜中，请检查后重新装配`)
    }
    return shot
  })
  const { shots: ordered, sources } = resolveSources(shots, store, 0)
  const clips = await buildClips(ordered, sources, duration, signal, { runId })
  commitAppend(store, previous, board, sources, clips, signal)
}
