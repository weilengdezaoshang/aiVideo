import type { CanvasDocData } from './doc-store.js'
import { migrateGenerationEntities } from './generation-entity.js'

export type GenerationStatus =
  'submitting' | 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | 'disconnected'

export type GenerationRuntime = {
  operationId: string
  generationId: string
  objectId: string
  transactionId: string
  jobId?: string
  status: GenerationStatus
  progress?: number
  message?: string
  error?: string
}

export type GenerationRuntimeStore = {
  get(operationId: string): GenerationRuntime | undefined
  getByObjectId(objectId: string): GenerationRuntime | undefined
  getByGenerationId(generationId: string): GenerationRuntime | undefined
  getByJobId(jobId: string): GenerationRuntime | undefined
  set(runtime: GenerationRuntime): void
  patch(operationId: string, patch: Partial<GenerationRuntime>): GenerationRuntime | undefined
  remove(operationId: string): void
  values(): GenerationRuntime[]
  subscribe(listener: () => void): () => boolean
}

export function createGenerationRuntimeStore(doc?: CanvasDocData): GenerationRuntimeStore {
  const entries = new Map<string, GenerationRuntime>()
  const listeners = new Set<() => void>()
  const notify = () => listeners.forEach((listener) => listener())
  const latestMatching = (predicate: (entry: GenerationRuntime) => boolean) => {
    const values = [...entries.values()]
    for (let index = values.length - 1; index >= 0; index -= 1) {
      if (predicate(values[index])) {
        return values[index]
      }
    }
    return undefined
  }

  // 文档迁移先于此处执行；运行态只恢复必要的任务索引，不保存业务结果。
  if (doc) {
    migrateGenerationEntities(doc)
    for (const object of Object.values(doc.objects)) {
      const entity = object.generationId ? doc.generations?.[object.generationId] : undefined
      if (entity && entity.status !== 'completed') {
        entries.set(entity.id, {
          operationId: entity.operationId,
          generationId: entity.id,
          objectId: object.id,
          transactionId: `legacy-${object.id}`,
          jobId: entity.jobId,
          status: entity.status,
        })
      }
    }
  }

  return {
    get: (operationId) => latestMatching((entry) => entry.operationId === operationId),
    getByObjectId: (objectId) => latestMatching((entry) => entry.objectId === objectId),
    getByGenerationId: (generationId) => entries.get(generationId),
    getByJobId: (jobId) => latestMatching((entry) => entry.jobId === jobId),
    set(runtime) {
      entries.set(runtime.generationId, { ...runtime })
      notify()
    },
    patch(operationId, patch) {
      const current = latestMatching((entry) => entry.operationId === operationId)
      if (!current) {
        return undefined
      }
      const next = { ...current, ...patch }
      entries.set(current.generationId, next)
      notify()
      return next
    },
    remove(operationId) {
      const current = latestMatching((entry) => entry.operationId === operationId)
      if (current && entries.delete(current.generationId)) {
        notify()
      }
    },
    values: () => [...entries.values()],
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
  }
}
