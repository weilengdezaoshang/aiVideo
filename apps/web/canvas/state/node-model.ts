import type { CanvasObj } from './commands.js'

type SizedNode = CanvasObj & { canvasSizeVersion?: 1 }

export type MediaKind = 'image' | 'video'
export type Reference = { assetId: string; ext: string; name: string; sourceNodeId?: string }
export type NodeDraft = {
  prompt: string
  model: string
  ratio: string
  resolution: number
  durationSec: number
  denoise: number
  references: Reference[]
}
export type NodeRun = {
  requestId: string
  jobId?: string
  /** 分镜服务端执行归属：由执行控制器统一调度与回填 */
  runId?: string
  status: 'submitting' | 'queued' | 'running' | 'failed' | 'uncertain'
  progress?: number
  message?: string
  snapshot: NodeDraft
}
export type Capability = {
  supported: boolean
  reason?: string
  models: { id: string; name: string }[]
  ratios: string[]
  resolutions: number[]
  /** 云端厂商的精确画幅档位(如 "1280x720");本地后端为空,回退 ratios×resolutions */
  sizes?: string[]
  referenceLimit: number
  durations?: number[]
  denoise?: boolean
}
export type Capabilities = { provider: string; image: Capability; video: Capability }
export type NodeJob = {
  id: string
  documentId?: string
  clientRef?: string
  requestId?: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  progress?: number
  message?: string
  error?: string
  images?: {
    id: string
    url: string
    file: string
    params: { kind: MediaKind; width: number; height: number }
  }[]
}

export function defaultDraft(kind: MediaKind): NodeDraft {
  return {
    prompt: '',
    model: '',
    ratio: kind === 'video' ? '16:9' : '1:1',
    resolution: kind === 'video' ? 512 : 1024,
    durationSec: 4,
    denoise: 0.6,
    references: [],
  }
}

export function createNode(
  kind: MediaKind | 'text',
  at: { x: number; y: number },
  name: string,
): SizedNode {
  return {
    id: `node-${crypto.randomUUID()}`,
    canvasSizeVersion: 1,
    kind,
    ...at,
    width: kind === 'video' ? 480 : 320,
    height: kind === 'video' ? 270 : 320,
    name,
    ...(kind === 'text'
      ? { text: '', fontSize: 16, textCard: true }
      : { nodeDraft: defaultDraft(kind) }),
  }
}

export function isMediaNode(
  obj: CanvasObj | undefined,
): obj is CanvasObj & { kind: MediaKind; nodeDraft: NodeDraft } {
  return !!obj && (obj.kind === 'image' || obj.kind === 'video') && !!obj.nodeDraft
}

export function isEmptyMedia(obj: CanvasObj): boolean {
  return (obj.kind === 'image' || obj.kind === 'video') && !obj.src && !obj.assetId
}

export function hasOutput(obj: CanvasObj): boolean {
  return Boolean(obj.src || obj.assetId)
}
export function isBusy(obj: CanvasObj): boolean {
  return (
    !!obj.nodeRun && ['submitting', 'queued', 'running', 'uncertain'].includes(obj.nodeRun.status)
  )
}

export function migrateNodes(objects: Record<string, CanvasObj>): boolean {
  let changed = false
  for (const obj of Object.values(objects)) {
    const sized = obj as SizedNode
    // 旧版本按视口反向放大过空节点，只迁移一次；后续手动缩放保留。
    if (!sized.canvasSizeVersion && (isMediaNode(obj) || obj.textCard)) {
      if (!hasOutput(obj) && !obj.text?.trim()) {
        obj.width = obj.kind === 'video' ? 480 : 320
        obj.height = obj.kind === 'video' ? 270 : 320
      }
      sized.canvasSizeVersion = 1
      changed = true
    }
    if (obj.kind === 'draft') {
      obj.kind = obj.mediaType === 'video' ? 'video' : 'image'
      obj.nodeDraft = defaultDraft(obj.kind)
      delete obj.mediaType
      changed = true
    }
  }
  return changed
}

export function outputSize(draft: NodeDraft): { width: number; height: number } {
  const [a, b] = draft.ratio.split(':').map(Number)
  const ratio = a > 0 && b > 0 ? a / b : 1
  const align = (value: number) => Math.max(64, Math.round(value / 8) * 8)
  return ratio >= 1
    ? { width: align(draft.resolution), height: align(draft.resolution / ratio) }
    : { width: align(draft.resolution * ratio), height: align(draft.resolution) }
}

export function applyNodeJob(obj: CanvasObj, job: NodeJob, docId: string): boolean {
  const run = obj.nodeRun
  if (
    !run ||
    job.documentId !== docId ||
    job.clientRef !== obj.id ||
    job.requestId !== run.requestId
  ) {
    return false
  }
  if (job.status === 'completed') {
    const result = job.images?.at(-1)
    if (!result) {
      obj.nodeRun = { ...run, status: 'failed', message: '任务完成但缺少结果，请重试' }
      return true
    }
    obj.src = result.url
    obj.imageId = result.id
    obj.ext = result.file.split('.').at(-1)
    obj.kind = result.params.kind
    obj.height = (obj.width * result.params.height) / result.params.width
    obj.lineage = {
      fromId: run.snapshot.references[0]?.sourceNodeId || '',
      params: { ...run.snapshot, kind: obj.kind },
    }
    delete obj.nodeRun
  } else if (job.status === 'failed') {
    if (job.error === '已取消') {
      delete obj.nodeRun
    } else {
      obj.nodeRun = { ...run, jobId: job.id, status: 'failed', message: job.error || '生成失败' }
    }
  } else {
    obj.nodeRun = {
      ...run,
      jobId: job.id,
      status: job.status,
      progress: job.progress,
      message: job.message,
    }
  }
  return true
}
