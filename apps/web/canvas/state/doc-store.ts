import type { Storyboard } from './storyboard.js'
import type { Timeline } from './timeline.js'
// DocStore:画布文档的可撤销状态(PRD §7.2)。
// - 用户命令 → apply() 进 undo 栈;
// - SSE/生成事件 → mutateTransient() 直接变更,绝不进栈;
// 订阅者拿到的总是同一 doc 引用(浅可变),监听 type 区分来源。

import { type CanvasCommand, type CanvasObj } from './commands.js'
import {
  migrateGenerationEntities,
  type GenerationEntity,
  type GenerationPatch,
} from './generation-entity.js'
import { createHistoryManager, type HistoryChange } from './history-manager.js'

/** 服务端规划回填的单方向元数据 */
export type GroupDirectionMeta = {
  slot: number
  title: string
  dimension: string
  prompt: string
  jobId: string
  clientRef: string
}

/** 随文档持久化的组元数据(标题 = 需求截断;方向待服务端规划回填) */
export type GroupMeta = {
  title: string
  prompt: string
  createdAt: string
  slots: string[]
  subject?: string
  constraints?: string[]
  directions?: GroupDirectionMeta[]
}

/** 对话栏消息(持久化在文档 chat 字段;异构消息共用一个宽接口,按 kind 区分渲染) */
export type ChatMessage = {
  id: string
  at: string
  role: 'user' | 'assistant' | 'system'
  kind: 'text' | 'group' | 'plan' | 'result'
  text?: string
  error?: boolean
  groupId?: string
  slotObjIds?: string[]
  prompt?: string
  expanded?: boolean
  sessionId?: string
  plan?: { steps?: { label: string }[] }
  request?: string
  assetThumb?: string
  waitingMask?: boolean
  completed?: boolean
  objId?: string
  maskUrl?: string
  planObjId?: string
  assetUrl?: string
  sourceLabel?: string
  pendingPlace?: boolean
  assetId?: string
  ext?: string
  width?: number
  height?: number
  hasThumbs?: boolean
}

export type CanvasDocData = {
  id: string
  name: string
  objects: Record<string, CanvasObj>
  order: string[]
  storyboard?: Storyboard
  timeline?: Timeline
  revision?: number
  groups?: Record<string, GroupMeta>
  chat?: ChatMessage[]
  generations?: Record<string, GenerationEntity>
}

export type DocStoreEvent = { type: 'command' | 'persistent' | 'transient'; doc: CanvasDocData }

export type DocStore = {
  doc: CanvasDocData
  subscribe(fn: (evt: DocStoreEvent) => void): () => boolean
  canUndo(): boolean
  canRedo(): boolean
  /** 用户命令:应用 + 入栈(可窗口内合并);清空 redo */
  apply(cmd: CanvasCommand, options?: { coalesce?: boolean }): void
  undo(): boolean
  redo(): boolean
  putGeneration(entity: GenerationEntity): void
  patchGeneration(
    generationId: string,
    operationId: string,
    patch: GenerationPatch,
    version: number,
  ): boolean
  subscribeHistory(fn: (change: HistoryChange) => void): () => boolean
  /** 瞬态变更(SSE 落位/进度/错误):不进 undo 栈;返回值用于通知 payload */
  mutateTransient<T>(mutator: (doc: CanvasDocData) => T): T
}

/** @param {CanvasDocData} initialDoc */
export function createDocStore(initialDoc: CanvasDocData): DocStore {
  const doc: CanvasDocData = {
    ...initialDoc,
    objects: { ...initialDoc.objects },
    order: [...initialDoc.order],
    generations: { ...(initialDoc.generations || {}) },
  }
  migrateGenerationEntities(doc)
  const listeners = new Set<(evt: DocStoreEvent) => void>()

  function notify(type: DocStoreEvent['type']) {
    for (const fn of listeners) {
      fn({ type, doc })
    }
  }

  const history = createHistoryManager(doc, { onDocumentChange: () => notify('command') })

  return {
    doc,
    subscribe(fn) {
      listeners.add(fn)
      return () => listeners.delete(fn)
    },
    canUndo: history.canUndo,
    canRedo: history.canRedo,
    subscribeHistory: history.subscribe,

    apply(cmd, { coalesce = true } = {}) {
      history.apply(cmd, { coalesce })
    },

    undo() {
      return history.undo()
    },

    redo() {
      return history.redo()
    },

    putGeneration(entity) {
      doc.generations ||= {}
      doc.generations[entity.id] = structuredClone(entity)
      notify('persistent')
    },

    patchGeneration(generationId, operationId, patch, version) {
      const entity = doc.generations?.[generationId]
      if (!entity || entity.operationId !== operationId || version <= entity.version) {
        return false
      }
      Object.assign(entity, structuredClone(patch), { version })
      notify('persistent')
      return true
    },

    mutateTransient(mutator) {
      const payload = mutator(doc)
      notify('transient')
      return payload
    },
  }
}
