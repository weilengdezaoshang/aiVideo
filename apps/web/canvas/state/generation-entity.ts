import type { CanvasObj } from './commands.js'
import type { CanvasDocData } from './doc-store.js'
import type { GenerationRuntimeStore } from './generation-runtime-store.js'
import { initializeGenerationFrame, outputSize } from './node-model.js'

export type GenerationSpec = {
  mode: 'text-to-image' | 'image-to-image'
  params: Record<string, unknown>
  sourceObjectId?: string
}

export type GenerationResult = {
  assetId?: string
  imageId?: string
  src: string
  ext: string
  kind: 'image' | 'video'
  width?: number
  height?: number
  hasThumbs?: boolean
}

export type GenerationEntityStatus =
  'uncertain' | 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'

export type GenerationEntity = {
  id: string
  operationId: string
  spec: GenerationSpec
  status: GenerationEntityStatus
  jobId?: string
  result?: GenerationResult
  error?: string
  version: number
}

export type GenerationPatch = Partial<
  Pick<GenerationEntity, 'status' | 'jobId' | 'result' | 'error'>
>

export function migrateGenerationEntities(doc: CanvasDocData): void {
  doc.generations = { ...(doc.generations || {}) }
  for (const object of Object.values(doc.objects)) {
    // Workspace documents from the preceding format used a square image placeholder.
    const initialDraft = object.nodeRun?.snapshot || object.nodeDraft
    if (initialDraft && !object.groupId && (object.nodeRun || object.generationId)) {
      const existing = object.generationId ? doc.generations[object.generationId] : undefined
      const kind =
        existing?.spec.params.kind === 'video' || object.kind === 'video' ? 'video' : 'image'
      const width = Number(existing?.spec.params.width)
      const height = Number(existing?.spec.params.height)
      const size = width > 0 && height > 0 ? { width, height } : outputSize(initialDraft)
      initializeGenerationFrame(object, size, kind)
    }
    if (object.generationId && doc.generations[object.generationId]) {
      if (Object.prototype.propertyIsEnumerable.call(object, 'gen')) {
        delete object.gen
      }
      delete object.generationRef
      delete object.generationSpec
      delete object.nodeRun
      continue
    }
    const legacyRuntime = object.gen ? { ...object.gen } : undefined
    // 工作台在跳转前保存 nodeRun；载入画布后与画布内提交共用实体和完成事件。
    const nodeRun = object.groupId ? undefined : object.nodeRun
    const operationId = object.generationRef?.operationId || nodeRun?.requestId
    const spec: GenerationSpec | undefined =
      object.generationSpec ||
      (nodeRun
        ? {
            mode: nodeRun.snapshot.references.length ? 'image-to-image' : 'text-to-image',
            params: {
              kind: object.kind === 'video' ? 'video' : 'image',
              prompt: nodeRun.snapshot.prompt,
              model: nodeRun.snapshot.model,
              ...outputSize(nodeRun.snapshot),
              durationSec: nodeRun.snapshot.durationSec,
              denoise: nodeRun.snapshot.denoise,
              referenceAssetIds: nodeRun.snapshot.references.map((reference) => reference.assetId),
            },
            sourceObjectId: nodeRun.snapshot.references[0]?.sourceNodeId,
          }
        : undefined)
    const belongsToLegacyWorkflow = Boolean(object.groupId || object.nodeDraft || object.nodeRun)
    if (
      !operationId &&
      !spec &&
      (belongsToLegacyWorkflow || !object.gen) &&
      !(object.kind === 'error' && object.lineage?.params && !belongsToLegacyWorkflow)
    ) {
      continue
    }
    const generationId = object.generationId || `generation-${operationId || object.id}`
    const completed = !nodeRun && (object.kind === 'image' || object.kind === 'video')
    doc.generations[generationId] = {
      id: generationId,
      operationId: operationId || `legacy-${object.id}`,
      spec: spec || {
        mode: object.lineage?.fromId ? 'image-to-image' : 'text-to-image',
        params: structuredClone(object.lineage?.params || {}) as Record<string, unknown>,
        ...(object.lineage?.fromId ? { sourceObjectId: object.lineage.fromId } : {}),
      },
      status:
        nodeRun && !nodeRun.jobId && ['submitting', 'uncertain'].includes(nodeRun.status)
          ? 'uncertain'
          : completed
            ? 'completed'
            : object.kind === 'error' || nodeRun?.status === 'failed'
              ? 'failed'
              : nodeRun?.status === 'running'
                ? 'running'
                : 'queued',
      jobId: object.generationRef?.jobId || object.gen?.jobId || nodeRun?.jobId,
      result:
        completed && object.src
          ? {
              kind: object.kind as 'image' | 'video',
              src: object.src,
              ext: object.ext || 'png',
              assetId: object.assetId,
              imageId: object.imageId,
              hasThumbs: object.hasThumbs,
              width: object.width,
              height: object.height,
            }
          : undefined,
      error: object.errorDetail || (nodeRun?.status === 'failed' ? nodeRun.message : undefined),
      version: 1,
    }
    object.generationId = generationId
    object.kind = 'placeholder'
    delete object.gen
    delete object.generationRef
    delete object.generationSpec
    delete object.nodeRun
    delete object.assetId
    delete object.imageId
    delete object.src
    delete object.ext
    delete object.hasThumbs
    delete object.errorDetail
    if (legacyRuntime) {
      Object.defineProperty(object, 'gen', {
        configurable: true,
        enumerable: false,
        writable: true,
        value: legacyRuntime,
      })
    }
  }
}

export function selectCanvasObject(
  doc: Pick<CanvasDocData, 'objects' | 'generations'>,
  objectOrId: CanvasObj | string,
  runtime?: GenerationRuntimeStore,
): CanvasObj | undefined {
  const object = typeof objectOrId === 'string' ? doc.objects[objectOrId] : objectOrId
  if (!object?.generationId) {
    return object
  }
  const entity = doc.generations?.[object.generationId]
  if (!entity) {
    return object
  }
  const view = { ...object }
  const live = runtime?.getByGenerationId(entity.id)
  if (entity.status === 'completed' && entity.result) {
    view.kind = entity.result.kind
    view.assetId = entity.result.assetId
    view.imageId = entity.result.imageId
    view.src = entity.result.src
    view.ext = entity.result.ext
    view.hasThumbs = entity.result.hasThumbs
  } else if (entity.status === 'failed' || entity.status === 'cancelled') {
    view.kind = 'error'
    view.errorDetail = entity.error || (entity.status === 'cancelled' ? '已取消' : '生成失败')
  } else {
    view.kind = 'placeholder'
    view.gen = {
      jobId: entity.jobId,
      progress: live?.progress,
      message:
        live?.message ||
        (entity.status === 'uncertain'
          ? '提交结果待确认'
          : entity.status === 'running'
            ? '生成中'
            : '排队中'),
    }
  }
  view.lineage =
    object.lineage ||
    (entity.spec.sourceObjectId
      ? { fromId: entity.spec.sourceObjectId, params: entity.spec.params }
      : undefined)
  return view
}

export function selectCanvasDocument(
  doc: CanvasDocData,
  runtime?: GenerationRuntimeStore,
): CanvasDocData {
  const objects: Record<string, CanvasObj> = {}
  for (const [id, object] of Object.entries(doc.objects)) {
    objects[id] = selectCanvasObject(doc, object, runtime) || object
  }
  return { ...doc, objects }
}
