// 兼容入口：UI 仍调用原函数，实际生成、运行态与历史补全统一交给 GenerationCoordinator。

import { cmdRemoveObjects, type CanvasObj } from '../state/commands.js'
import type { DocStore } from '../state/doc-store.js'
import { selectCanvasObject } from '../state/generation-entity.js'
import {
  createGenerationCoordinator,
  type GenerationCoordinator,
} from '../state/generation-coordinator.js'

const coordinators = new WeakMap<DocStore, GenerationCoordinator>()

export function getGenerationCoordinator(
  docStore: DocStore,
  options: { fetchImpl?: typeof fetch } = {},
): GenerationCoordinator {
  const existing = coordinators.get(docStore)
  if (existing) {
    return existing
  }
  const coordinator = createGenerationCoordinator({
    docStore,
    ...(options.fetchImpl ? { fetchImpl: options.fetchImpl } : {}),
  })
  coordinators.set(docStore, coordinator)
  return coordinator
}

export type GenerateResult =
  | {
      ok: true
      objId: string
      generationId: string
      jobId?: string
      operationId: string
      transactionId: string
    }
  | {
      ok: false
      objId?: string
      generationId?: string
      error: string
      operationId?: string
      transactionId?: string
    }

export type SubmitGenerationOptions = {
  docStore: DocStore
  genParams: Record<string, unknown>
  anchor?: { x: number; y: number }
  initImage?: { dataUrl: string }
  maskImage?: { dataUrl: string }
  lineage?: { fromId: string; params?: object }
  objectId?: string
  onPlaceholderCreated?: (placeholder: CanvasObj) => void
  fetchImpl?: typeof fetch
}

export async function submitGeneration(options: SubmitGenerationOptions): Promise<GenerateResult> {
  const coordinator = getGenerationCoordinator(options.docStore, { fetchImpl: options.fetchImpl })
  const result = await coordinator.submit({
    genParams: options.genParams,
    anchor: options.anchor,
    initImage: options.initImage,
    maskImage: options.maskImage,
    lineage: options.lineage,
    objectId: options.objectId,
    onPlaceholderCreated: options.onPlaceholderCreated,
  })
  return result.ok
    ? {
        ok: true,
        objId: result.objectId,
        generationId: result.generationId,
        jobId: result.jobId,
        operationId: result.operationId,
        transactionId: result.transactionId,
      }
    : {
        ok: false,
        objId: result.objectId,
        generationId: result.generationId,
        error: result.error || '生成失败',
        operationId: result.operationId,
        transactionId: result.transactionId,
      }
}

/** 显式取消会停止副作用并移除节点；Undo 生成事务时协调器也会自动取消。 */
export async function cancelPlaceholder(options: {
  docStore: DocStore
  objId: string
  fetchImpl?: typeof fetch
}) {
  const coordinator = getGenerationCoordinator(options.docStore, { fetchImpl: options.fetchImpl })
  const beforeCancel = options.docStore.doc.objects[options.objId]
    ? structuredClone(options.docStore.doc.objects[options.objId])
    : undefined
  const runtime = coordinator.runtime.getByObjectId(options.objId)
  if (runtime) {
    await coordinator.cancel(runtime.generationId)
  }
  const object = options.docStore.doc.objects[options.objId]
  if (object) {
    options.docStore.apply(
      cmdRemoveObjects([
        {
          object: beforeCancel || structuredClone(object),
          orderIndex: options.docStore.doc.order.indexOf(object.id),
        },
      ]),
      { coalesce: false },
    )
  }
}

/** 错误卡重试是一次新的显式生成事务。 */
export async function retryError(options: {
  docStore: DocStore
  objId: string
  resolveReference?: (fromId: string) => Promise<{ dataUrl: string } | null>
  fetchImpl?: typeof fetch
}): Promise<GenerateResult> {
  getGenerationCoordinator(options.docStore, { fetchImpl: options.fetchImpl })
  const placement = options.docStore.doc.objects[options.objId]
  const object = placement && selectCanvasObject(options.docStore.doc, placement)
  if (!placement || !object || object.kind !== 'error' || !placement.generationId) {
    return { ok: false, error: '对象不存在或不可重试' }
  }
  const entity = options.docStore.doc.generations?.[placement.generationId]
  const params = entity?.spec.params || object.lineage?.params
  if (!params) {
    return { ok: false, error: '缺少谱系参数,无法重试' }
  }
  const sourceObjectId = entity?.spec.sourceObjectId || object.lineage?.fromId
  let initImage: { dataUrl: string } | undefined
  if (sourceObjectId && options.resolveReference) {
    initImage = (await options.resolveReference(sourceObjectId).catch(() => null)) || undefined
  }
  return submitGeneration({
    docStore: options.docStore,
    // 重试保持节点身份稳定，但作为新的显式事务进入历史。
    genParams: { ...params },
    anchor: { x: object.x, y: object.y },
    initImage,
    lineage: sourceObjectId ? { fromId: sourceObjectId, params } : undefined,
    fetchImpl: options.fetchImpl,
    objectId: object.id,
  })
}
