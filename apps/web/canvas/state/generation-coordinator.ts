import { cmdGenerateNode, type CanvasCommand, type CanvasObj } from './commands.js'
import type { DocStore } from './doc-store.js'
import type { GenerationEntity, GenerationSpec } from './generation-entity.js'
import {
  createGenerationRuntimeStore,
  type GenerationRuntimeStore,
  type GenerationStatus,
} from './generation-runtime-store.js'
import {
  imageRecordToAsset,
  type JobImageRecord,
  type JobLike,
  type ResolvedImage,
} from './generation-reducer.js'
import { findFreePosition } from './placement.js'

let operationSequence = 0

export type GenerationRequest = {
  genParams: Record<string, unknown>
  anchor?: { x: number; y: number }
  initImage?: { dataUrl: string }
  maskImage?: { dataUrl: string }
  lineage?: { fromId: string; params?: object }
  objectId?: string
  additionalCommands?: CanvasCommand[]
  onPlaceholderCreated?: (placeholder: CanvasObj) => void
}

export type GenerationHandle = {
  ok: boolean
  generationId: string
  objectId: string
  operationId: string
  transactionId: string
  jobId?: string
  error?: string
}

export type GenerationEvent = {
  generationId?: string
  operationId?: string
  objectId?: string
  jobId: string
  status: GenerationStatus
  version?: number
  progress?: number
  message?: string
  error?: string
  image?: JobImageRecord
}

export type GenerationCoordinator = {
  readonly runtime: GenerationRuntimeStore
  submit(request: GenerationRequest): Promise<GenerationHandle>
  cancel(generationId: string): Promise<void>
  retry(generationId: string): Promise<GenerationHandle>
  acceptEvent(event: GenerationEvent): void
  acceptJob(job: JobLike): boolean
  dispose(): void
}

export type GenerationCoordinatorOptions = {
  docStore: DocStore
  fetchImpl?: typeof fetch
  runtime?: GenerationRuntimeStore
  resolveImage?: (image: JobImageRecord) => ResolvedImage | null
}

function id(prefix: string) {
  operationSequence += 1
  return `${prefix}-${Date.now().toString(36)}-${operationSequence}`
}

function entityStatus(status: GenerationStatus): GenerationEntity['status'] {
  if (status === 'submitting' || status === 'disconnected') {
    return 'queued'
  }
  return status
}

export function createGenerationCoordinator(
  options: GenerationCoordinatorOptions,
): GenerationCoordinator {
  const { docStore } = options
  const fetchImpl = options.fetchImpl || ((...args: Parameters<typeof fetch>) => fetch(...args))
  const runtime = options.runtime || createGenerationRuntimeStore(docStore.doc)
  const resolveImage = options.resolveImage || imageRecordToAsset
  const coordinator: GenerationCoordinator = {
    runtime,
    async submit(request) {
      const operationId = id('op')
      const generationId = id('generation')
      const transactionId = id('tx')
      const objectId = request.objectId || id('obj')
      const width = Number(request.genParams.width) || 512
      const height = Number(request.genParams.height) || 512
      const existingObject = request.objectId ? docStore.doc.objects[request.objectId] : undefined
      const position = existingObject
        ? { x: existingObject.x, y: existingObject.y }
        : findFreePosition(docStore.doc, request.anchor || { x: 0, y: 0 }, { width, height })
      const spec: GenerationSpec = {
        mode: request.initImage ? 'image-to-image' : 'text-to-image',
        params: structuredClone(request.genParams),
        ...(request.lineage?.fromId ? { sourceObjectId: request.lineage.fromId } : {}),
      }
      const entity: GenerationEntity = {
        id: generationId,
        operationId,
        spec,
        status: 'queued',
        version: 1,
      }
      const placement: CanvasObj = {
        id: objectId,
        kind: 'placeholder',
        generationId,
        x: position.x,
        y: position.y,
        width,
        height,
        ...(request.lineage ? { lineage: { params: {}, ...request.lineage } } : {}),
      }
      docStore.putGeneration(entity)
      const generateCommand = cmdGenerateNode({
        transactionId,
        objectId,
        generationId,
        initialPlacement: placement,
      })
      docStore.apply(
        request.additionalCommands?.length
          ? { type: 'batch', commands: [generateCommand, ...request.additionalCommands] }
          : generateCommand,
        { coalesce: false },
      )
      request.onPlaceholderCreated?.(structuredClone(placement))
      runtime.set({
        operationId,
        generationId,
        objectId,
        transactionId,
        status: 'submitting',
        message: '正在提交',
      })

      try {
        const response = await fetchImpl('/api/generate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            ...request.genParams,
            batchCount: 1,
            clientRef: objectId,
            initImage: request.initImage?.dataUrl,
            maskImage: request.maskImage?.dataUrl,
          }),
        })
        const body = (await response.json().catch(() => ({}))) as { error?: string; jobId?: string }
        if (!response.ok) {
          coordinator.acceptEvent({
            generationId,
            operationId,
            objectId,
            jobId: body.jobId || '',
            status: 'failed',
            error: body.error || `提交失败 HTTP ${response.status}`,
          })
          return {
            ok: false,
            generationId,
            objectId,
            operationId,
            transactionId,
            error: body.error,
          }
        }
        const current = runtime.get(operationId)
        const terminal = current && ['completed', 'failed', 'cancelled'].includes(current.status)
        runtime.patch(
          operationId,
          terminal
            ? { jobId: current.jobId || body.jobId }
            : { jobId: body.jobId, status: 'queued', progress: 0, message: '排队中' },
        )
        const currentEntity = docStore.doc.generations?.[generationId]
        if (currentEntity && !terminal) {
          docStore.patchGeneration(
            generationId,
            operationId,
            { jobId: body.jobId, status: 'queued' },
            currentEntity.version + 1,
          )
        }
        if (current?.status === 'cancelled' && body.jobId) {
          void fetchImpl(`/api/jobs/${body.jobId}`, { method: 'DELETE' }).catch(() => undefined)
        }
        return {
          ok: true,
          generationId,
          objectId,
          operationId,
          transactionId,
          jobId: body.jobId,
        }
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error)
        coordinator.acceptEvent({
          generationId,
          operationId,
          objectId,
          jobId: '',
          status: 'failed',
          error: message || '网络错误',
        })
        return {
          ok: false,
          generationId,
          objectId,
          operationId,
          transactionId,
          error: message,
        }
      }
    },
    async cancel(generationOrOperationId) {
      const activeByOperation = runtime.get(generationOrOperationId)
      const generationId = activeByOperation?.generationId || generationOrOperationId
      const entity = docStore.doc.generations?.[generationId]
      if (!entity) {
        if (activeByOperation?.jobId) {
          await fetchImpl(`/api/jobs/${activeByOperation.jobId}`, { method: 'DELETE' }).catch(
            () => undefined,
          )
        }
        return
      }
      const active = runtime.getByGenerationId(generationId)
      runtime.patch(entity.operationId, { status: 'cancelled', message: '已取消' })
      docStore.patchGeneration(
        generationId,
        entity.operationId,
        { status: 'cancelled', error: '已取消' },
        entity.version + 1,
      )
      const jobId = active?.jobId || entity.jobId
      if (jobId) {
        await fetchImpl(`/api/jobs/${jobId}`, { method: 'DELETE' }).catch(() => undefined)
      }
    },
    async retry(generationId) {
      const entity = docStore.doc.generations?.[generationId]
      const placement = Object.values(docStore.doc.objects).find(
        (object) => object.generationId === generationId,
      )
      if (!entity || !placement) {
        return {
          ok: false,
          generationId,
          objectId: placement?.id || '',
          operationId: entity?.operationId || '',
          transactionId: '',
          error: '缺少生成实体或画布节点，无法重试',
        }
      }
      return coordinator.submit({
        genParams: entity.spec.params,
        anchor: { x: placement.x, y: placement.y },
        lineage: entity.spec.sourceObjectId
          ? { fromId: entity.spec.sourceObjectId, params: entity.spec.params }
          : undefined,
        objectId: placement.id,
      })
    },
    acceptEvent(event) {
      const active = event.generationId
        ? runtime.getByGenerationId(event.generationId)
        : event.operationId
          ? runtime.get(event.operationId)
          : event.objectId
            ? runtime.getByObjectId(event.objectId)
            : runtime.getByJobId(event.jobId)
      if (!active || (active.jobId && event.jobId && active.jobId !== event.jobId)) {
        return
      }
      const entity = docStore.doc.generations?.[active.generationId]
      if (!entity || entity.operationId !== active.operationId) {
        return
      }
      if (event.version !== undefined && event.version <= entity.version) {
        return
      }
      runtime.patch(active.operationId, {
        jobId: active.jobId || event.jobId || undefined,
        status: event.status,
        progress: event.progress,
        message: event.message,
        error: event.error,
      })
      const resolved =
        event.status === 'completed' && event.image ? resolveImage(event.image) : null
      if (event.status === 'completed' && !resolved) {
        return
      }
      const duplicate =
        entity.status === entityStatus(event.status) &&
        (!resolved || entity.result?.imageId === resolved.imageId) &&
        (!event.jobId || entity.jobId === event.jobId)
      if (duplicate) {
        return
      }
      const version = event.version ?? entity.version + 1
      const changed = docStore.patchGeneration(
        entity.id,
        entity.operationId,
        {
          status: entityStatus(event.status),
          jobId: entity.jobId || event.jobId || undefined,
          error:
            event.error || (event.status === 'failed' ? event.message || '生成失败' : undefined),
          result: resolved
            ? {
                kind: resolved.kind,
                assetId: resolved.assetId || undefined,
                src: resolved.src,
                imageId: resolved.imageId,
                hasThumbs: resolved.hasThumbs,
                ext: resolved.ext,
                width: resolved.width,
                height: resolved.height,
              }
            : entity.result,
        },
        version,
      )
      if (!changed) {
        return
      }
    },
    acceptJob(job) {
      // 重试会复用 placement id；已知 jobId 时先命中对应的历史运行记录。
      // 仅在 SSE 早于 POST 响应时，才回退到当前 objectId 索引。
      const active =
        runtime.getByJobId(job.id) ||
        (job.clientRef ? runtime.getByObjectId(job.clientRef) : undefined)
      if (!active) {
        return false
      }
      const status: GenerationStatus =
        job.status === 'completed'
          ? 'completed'
          : job.status === 'failed'
            ? 'failed'
            : job.status === 'running'
              ? 'running'
              : 'queued'
      coordinator.acceptEvent({
        generationId: active.generationId,
        operationId: active.operationId,
        objectId: active.objectId,
        jobId: job.id,
        status,
        progress: job.progress,
        message: job.message,
        error: job.error,
        image: job.images?.[job.images.length - 1],
      })
      return true
    },
    dispose() {},
  }
  return coordinator
}
