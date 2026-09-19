/** 镜头计划独立于剪辑片段；nodeId 关联选用的生成节点。 */
export type Shot = {
  id: string
  title: string
  visual: string
  dialogue: string
  camera: string
  durationFrames: number
  nodeId: string | null
  versions?: string[]
  locked: boolean
}
export type Storyboard = { version: 1; outline: string; shots: Shot[] }
export function emptyStoryboard(): Storyboard {
  return { version: 1, outline: '', shots: [] }
}
export function newShot(): Shot {
  return {
    id: crypto.randomUUID(),
    title: '新镜头',
    visual: '',
    dialogue: '',
    camera: '',
    durationFrames: 150,
    nodeId: null,
    locked: false,
  }
}

export function isStoryboard(value: unknown): value is Storyboard {
  if (!value || typeof value !== 'object') {
    return false
  }
  const raw = value as Record<string, unknown>
  if (
    raw.version !== 1 ||
    typeof raw.outline !== 'string' ||
    raw.outline.length > 20000 ||
    !Array.isArray(raw.shots) ||
    raw.shots.length > 200
  ) {
    return false
  }
  const ids = new Set<string>()
  let total = 0
  for (const item of raw.shots as unknown[]) {
    if (!item || typeof item !== 'object') {
      return false
    }
    const shot = item as Record<string, unknown>
    if (typeof shot.id !== 'string' || !shot.id || shot.id.length > 100 || ids.has(shot.id)) {
      return false
    }
    ids.add(shot.id)
    for (const [field, limit] of [
      ['title', 500],
      ['visual', 4000],
      ['dialogue', 4000],
      ['camera', 1000],
    ] as const) {
      if (typeof shot[field] !== 'string' || shot[field].length > limit) {
        return false
      }
    }
    if (
      typeof shot.durationFrames !== 'number' ||
      !Number.isInteger(shot.durationFrames) ||
      shot.durationFrames < 1 ||
      shot.durationFrames > 108000 ||
      typeof shot.locked !== 'boolean'
    ) {
      return false
    }
    if (
      shot.nodeId !== null &&
      (typeof shot.nodeId !== 'string' || !shot.nodeId || shot.nodeId.length > 100)
    ) {
      return false
    }
    if (
      shot.versions !== undefined &&
      (!Array.isArray(shot.versions) ||
        shot.versions.length > 100 ||
        shot.versions.some((id) => typeof id !== 'string' || !id || id.length > 100) ||
        new Set(shot.versions).size !== shot.versions.length)
    ) {
      return false
    }
    total += shot.durationFrames
  }
  return total <= 108000
}
