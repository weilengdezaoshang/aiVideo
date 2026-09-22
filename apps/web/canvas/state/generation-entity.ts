import type { CanvasObj } from './commands.js'
import type { CanvasDocData } from './doc-store.js'
import type { GenerationRuntimeStore } from './generation-runtime-store.js'

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

export type GenerationEntityStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'

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
    if (object.generationId && doc.generations[object.generationId]) {
      if (Object.prototype.propertyIsEnumerable.call(object, 'gen')) {
        delete object.gen
      }
      delete object.generationRef
      delete object.generationSpec
      continue
    }
    const legacyRuntime = object.gen ? { ...object.gen } : undefined
    const operationId = object.generationRef?.operationId
    const spec = object.generationSpec
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
    const completed = object.kind === 'image' || object.kind === 'video'
    doc.generations[generationId] = {
      id: generationId,
      operationId: operationId || `legacy-${object.id}`,
      spec: spec || {
        mode: object.lineage?.fromId ? 'image-to-image' : 'text-to-image',
        params: structuredClone(object.lineage?.params || {}) as Record<string, unknown>,
        ...(object.lineage?.fromId ? { sourceObjectId: object.lineage.fromId } : {}),
      },
      status: completed ? 'completed' : object.kind === 'error' ? 'failed' : 'queued',
      jobId: object.generationRef?.jobId || object.gen?.jobId,
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
      error: object.errorDetail,
      version: 1,
    }
    object.generationId = generationId
    object.kind = 'placeholder'
    delete object.gen
    delete object.generationRef
    delete object.generationSpec
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
      message: live?.message || (entity.status === 'running' ? '生成中' : '排队中'),
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
