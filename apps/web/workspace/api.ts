import {
  createNode,
  outputSize,
  type Capabilities,
  type MediaKind,
  type NodeDraft,
  type NodeJob,
  type Reference,
} from '../canvas/state/node-model.js'
import type { CanvasObj } from '../canvas/state/commands.js'
import type { Project } from '../ui/media-cards.js'

export type CanvasDocument = {
  id: string
  name: string
  revision: number
  updatedAt: string
  objects: Record<string, CanvasObj>
  order: string[]
}
export type AssetEntry = {
  asset: {
    id: string
    kind: 'image' | 'video'
    ext: string
    width: number
    height: number
    createdAt: string
    name?: string
  }
  urls: { original: string; thumb256?: string | null; thumb1024?: string | null }
}
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
  }
}
export async function request<T>(
  path: string,
  options: RequestInit = {},
  fetcher: typeof fetch = fetch,
): Promise<T> {
  const response = await fetcher(path, {
    ...options,
    signal: options.signal ?? AbortSignal.timeout(30000),
  })
  const body: unknown = await response.json()
  if (!response.ok) {
    const message =
      typeof body === 'object' && body !== null && 'error' in body && typeof body.error === 'string'
        ? body.error
        : `请求失败（${response.status}）`
    throw new ApiError(message, response.status)
  }
  return body as T // Typed boundary for the repository's JSON API; never propagated as any.
}
export const json = (body: unknown): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})
export const getCapabilities = (signal?: AbortSignal) =>
  request<Capabilities>('/api/generation-capabilities', { signal })
export const getDocuments = (signal?: AbortSignal) =>
  request<{ documents: Project[] }>('/api/documents', { signal })
export const getAssets = (signal?: AbortSignal) =>
  request<{ assets: AssetEntry[] }>('/api/assets?limit=200', { signal })
export async function uploadReference(
  file: File,
  signal?: AbortSignal,
): Promise<Reference & { url: string }> {
  const types: Record<string, string> = {
    'image/jpeg': 'jpg',
    'image/png': 'png',
    'image/webp': 'webp',
  }
  const ext = types[file.type]
  if (!ext || file.size > 8 * 1024 * 1024 || !file.size) {
    throw new Error('请选择 8MB 以内的 JPG、PNG 或 WebP 图片')
  }
  const result = await request<AssetEntry>(`/api/assets?ext=${ext}`, {
    method: 'POST',
    body: file,
    headers: { 'Content-Type': file.type },
    signal,
  })
  return {
    assetId: result.asset.id,
    ext: result.asset.ext,
    name: file.name,
    url: result.urls.thumb256 || result.urls.original,
  }
}
export function validateGeneration(
  kind: MediaKind,
  draft: NodeDraft,
  caps: Capabilities,
): string | null {
  const cap = caps[kind]
  if (!cap.supported) {
    return cap.reason || '当前后端不支持此模式'
  }
  if (!draft.prompt.trim()) {
    return '请先描述你想创作的画面'
  }
  if (!cap.models.some((model) => model.id === draft.model)) {
    return '请选择可用模型'
  }
  if (!cap.ratios.includes(draft.ratio) || !cap.resolutions.includes(draft.resolution)) {
    return '当前模型不支持所选尺寸'
  }
  if (draft.references.length > cap.referenceLimit) {
    return '当前后端不支持所选参考图数量'
  }
  if (kind === 'video' && !draft.references.length) {
    return '图生视频需要上传一张首帧参考图'
  }
  if (kind === 'video' && !cap.durations?.includes(draft.durationSec)) {
    return '请选择支持的视频时长'
  }
  return null
}

/** One immutable intent survives uncertain HTTP results. Saving the node precedes submission so
 * the existing canvas SSE/reconciliation flow can always recover it, including after navigation. */

/** 提示词截断为画布/文档名;截断时补省略号,避免出现无提示的怪异断句 */
function truncateLabel(text: string, limit = 24): string {
  const trimmed = text.trim()
  return trimmed.length > limit ? `${trimmed.slice(0, limit)}…` : trimmed
}

export class CreationIntent {
  private readonly key = `workspace-${crypto.randomUUID()}`
  private readonly requestId = crypto.randomUUID()
  private node?: ReturnType<typeof createNode>
  private document?: CanvasDocument
  private prepared = false
  constructor(
    private readonly kind?: MediaKind,
    private readonly draft?: NodeDraft,
    private readonly fetcher: typeof fetch = fetch,
  ) {
    if (kind && draft) {
      this.node = createNode(kind, { x: 80, y: 80 }, truncateLabel(draft.prompt))
      this.node.nodeDraft = structuredClone(draft)
      this.node.nodeRun = {
        requestId: this.requestId,
        status: 'submitting',
        snapshot: structuredClone(draft),
      }
    }
  }
  get documentId() {
    return this.document?.id
  }
  async run(): Promise<string> {
    if (!this.document) {
      const result = await request<{ document: CanvasDocument }>(
        '/api/documents',
        {
          ...json({ name: this.draft ? truncateLabel(this.draft.prompt) : undefined }),
          headers: { 'Content-Type': 'application/json', 'Idempotency-Key': this.key },
        },
        this.fetcher,
      )
      this.document = result.document
    }
    const doc = this.document
    if (!this.node || !this.draft || !this.kind) {
      return doc.id
    }
    if (!this.prepared) {
      // Re-read after an uncertain save. Never overwrite a canvas already changed in another tab.
      const { document: latest } = await request<{ document: CanvasDocument }>(
        `/api/documents/${doc.id}`,
        {},
        this.fetcher,
      )
      if (latest.objects[this.node.id]) {
        const existing = latest.objects[this.node.id]
        if (existing.nodeRun?.requestId !== this.requestId) {
          throw new Error('该画布已更新，请打开画布继续创作')
        }
      } else {
        if (latest.order.length) {
          throw new Error('该画布已有内容，请打开画布继续创作')
        }
        await request(
          `/api/documents/${doc.id}`,
          json({
            name: doc.name,
            baseRevision: latest.revision,
            objects: { [this.node.id]: this.node },
            order: [this.node.id],
          }),
          this.fetcher,
        )
      }
      this.prepared = true
    }
    await request<{ job: NodeJob }>(
      '/api/generate',
      json({
        documentId: doc.id,
        clientRef: this.node.id,
        requestId: this.requestId,
        kind: this.kind,
        prompt: this.draft.prompt,
        model: this.draft.model,
        ...outputSize(this.draft),
        durationSec: this.draft.durationSec,
        fps: 16,
        denoise: this.draft.denoise,
        batchCount: 1,
        steps: 20,
        cfgScale: 7,
        seed: -1,
        sampler: 'euler',
        scheduler: 'normal',
        referenceAssetIds: this.draft.references.map((ref) => ref.assetId),
      }),
      this.fetcher,
    )
    return doc.id
  }
}
