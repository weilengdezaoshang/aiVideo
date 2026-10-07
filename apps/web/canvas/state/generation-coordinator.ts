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
import { latestJob } from './job-version.js'
import { writeLocalDraft } from './document-api.js'

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
  recover(generationId: string): Promise<GenerationHandle>
  retry(generationId: string): Promise<GenerationHandle>
  acceptEvent(event: GenerationEvent): void
  acceptJob(job: JobLike): boolean
  dispose(): void
}

export type GenerationCoordinatorOptions = {
  docStore: DocStore
  fetchImpl?: typeof fetch
  runtime?: GenerationRuntimeStore
  ensureSaved?: () => Promise<void | number>
  resolveImage?: (image: JobImageRecord) => ResolvedImage | null
}

function id(prefix: string) {
  return `${prefix}-${crypto.randomUUID()}`
}

function entityStatus(status: GenerationStatus): GenerationEntity['status'] {
  if (status === 'submitting') {
    return 'uncertain'
  }
  if (status === 'disconnected') {
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
  const versions = new Map<string, JobLike>()
  const pendingMessage = '提交结果待确认，请查询原请求或取消后重新生成'
  const handle = (generationId: string, error?: string): GenerationHandle => {
    const entry = runtime.getByGenerationId(generationId)
    return {
      ok: !error && !!entry?.jobId,
      generationId,
      objectId: entry?.objectId || '',
      operationId: entry?.operationId || '',
      transactionId: entry?.transactionId || '',
      jobId: entry?.jobId,
      error,
    }
  }
  const coordinator: GenerationCoordinator = {
    runtime,
    async submit(request) {
      const pending = runtime
        .values()
        .find(
          (entry) =>
            !entry.jobId &&
            ['submitting', 'uncertain', 'queued', 'disconnected'].includes(entry.status),
        )
      if (pending) {
        return handle(pending.generationId, pendingMessage)
      }
      const operationId = crypto.randomUUID()
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
        status: 'uncertain',
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

      let submitted = false
      try {
        // Persist the request identity before its side effect; a reload can query the same intent.
        writeLocalDraft(docStore.doc.id, docStore.doc)
        if (options.ensureSaved) {
          await options.ensureSaved()
        }
        if (runtime.get(operationId)?.status === 'cancelled') {
          return handle(generationId, '已取消')
        }
        submitted = true
        const response = await fetchImpl('/api/generate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            ...request.genParams,
            batchCount: 1,
            requestId: operationId,
            documentId: docStore.doc.id,
            clientRef: objectId,
            initImage: request.initImage?.dataUrl,
            maskImage: request.maskImage?.dataUrl,
          }),
        })
        const body = (await response.json().catch(() => ({}))) as { error?: string; jobId?: string }
        if (response.status >= 500 || response.status === 408) {
          throw new Error(body.error || '服务端受理结果未知')
        }
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
        if (!body.jobId) {
          throw new Error('提交响应缺少任务 ID')
        }
        const current = runtime.get(operationId)
        const terminal = current && ['completed', 'failed', 'cancelled'].includes(current.status)
        const receivedEvent = terminal || Boolean(current?.jobId)
        runtime.patch(
          operationId,
          receivedEvent
            ? { jobId: current?.jobId || body.jobId }
            : { jobId: body.jobId, status: 'queued', progress: 0, message: '排队中' },
        )
        const currentEntity = docStore.doc.generations?.[generationId]
        if (currentEntity && !receivedEvent) {
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
        const current = runtime.get(operationId)
        if (current?.jobId) {
          return handle(generationId)
        }
        if (current?.status === 'cancelled') {
          return handle(generationId, '已取消')
        }
        const message = submitted
          ? pendingMessage
          : error instanceof Error
            ? error.message
            : '保存生成请求失败'
        coordinator.acceptEvent({
          generationId,
          operationId,
          objectId,
          jobId: '',
          status: submitted ? 'uncertain' : 'failed',
          error: message,
          message,
        })
        if (submitted) {
          return coordinator.recover(generationId)
        }
        return handle(generationId, message)
      }
    },
    async recover(generationId) {
      const entry = runtime.getByGenerationId(generationId)
      if (!entry) {
        return handle(generationId, '未找到生成请求')
      }
      try {
        const response = await fetchImpl(
          entry.jobId
            ? `/api/jobs/${encodeURIComponent(entry.jobId)}`
            : `/api/generation-requests/${encodeURIComponent(entry.operationId)}`,
        )
        const body = (await response.json()) as { job?: JobLike; cancelled?: boolean }
        if (response.ok && body.job) {
          coordinator.acceptJob(body.job)
          return handle(generationId)
        }
        if (response.ok && body.cancelled) {
          coordinator.acceptEvent({ generationId, jobId: '', status: 'cancelled', error: '已取消' })
          return handle(generationId, '已取消')
        }
      } catch {
        /* A failed lookup does not prove that the original POST failed. */
      }
      return handle(generationId, pendingMessage)
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
      if (entity.status === 'completed') {
        return
      }
      const active = runtime.getByGenerationId(generationId)
      const jobId = active?.jobId || entity.jobId
      const response = await fetchImpl(
        jobId
          ? `/api/jobs/${encodeURIComponent(jobId)}`
          : `/api/generation-requests/${encodeURIComponent(entity.operationId)}`,
        { method: 'DELETE' },
      )
      if (!response.ok) {
        throw new Error('取消未确认，请重试取消')
      }
      // Only a confirmed cancellation releases an uncertain request for a new submission.
      coordinator.acceptEvent({
        generationId,
        jobId: jobId || '',
        status: 'cancelled',
        error: '已取消',
        message: '已取消',
      })
    },
    async retry(generationId) {
      const entry = runtime.getByGenerationId(generationId)
      if (entry && !['failed', 'cancelled', 'completed'].includes(entry.status)) {
        return coordinator.recover(generationId)
      }
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
                seed: resolved.seed,
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
      if (job.documentId && job.documentId !== docStore.doc.id) {
        return false
      }
      if (latestJob(versions, job) !== job) {
        return true
      }
      // 重试会复用 placement id；已知 jobId 时先命中对应的历史运行记录。
      // 仅在 SSE 早于 POST 响应时，才回退到当前 objectId 索引。
      const active =
        runtime.getByJobId(job.id) ||
        (job.clientRef ? runtime.getByObjectId(job.clientRef) : undefined)
      if (!active || (!active.jobId && job.requestId && active.operationId !== job.requestId)) {
        return false
      }
      const status: GenerationStatus =
        job.status === 'completed'
          ? 'completed'
          : job.status === 'failed'
            ? 'failed'
            : job.status === 'cancelled'
              ? 'cancelled'
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
