// DocStore:画布文档的可撤销状态(PRD §7.2)。
// - 用户命令 → apply() 进 undo 栈;
// - SSE/生成事件 → mutateTransient() 直接变更,绝不进栈;
// 订阅者拿到的总是同一 doc 引用(浅可变),监听 type 区分来源。

import { applyCommand, canCoalesce, coalesceInto, invertCommand } from './commands.js'

/**
 * @typedef {import('./commands.js').CanvasObj} CanvasObj
 * @typedef {{ id: string, name: string, objects: Record<string, CanvasObj>, order: string[], revision?: number, groups?: Record<string, any>, chat?: any[] }} CanvasDocData
 * @typedef {{ type: 'command' | 'transient', doc: CanvasDocData }} DocStoreEvent
 */

/** 同一交互的命令合并窗口(毫秒):窗口内同组对象的移动并成一步 undo */
const COALESCE_WINDOW_MS = 600
const UNDO_LIMIT = 100

/**
 * @param {CanvasDocData} initialDoc
 */
export function createDocStore(initialDoc) {
  const doc = { ...initialDoc, objects: { ...initialDoc.objects }, order: [...initialDoc.order] }
  const listeners = new Set()
  /** @type {any[]} */
  const undoStack = []
  /** @type {any[]} */
  const redoStack = []
  let lastAppliedAt = 0

  /** @param {DocStoreEvent['type']} type */
  function notify(type) {
    for (const fn of listeners) {
      fn({ type, doc })
    }
  }

  return {
    doc,
    /**
     * @param {(evt: DocStoreEvent) => void} fn
     */
    subscribe(fn) {
      listeners.add(fn)
      return () => listeners.delete(fn)
    },
    canUndo: () => undoStack.length > 0,
    canRedo: () => redoStack.length > 0,

    /**
     * 用户命令:应用 + 入栈(可窗口内合并);清空 redo
     * @param {any} cmd @param {{ coalesce?: boolean }} [options]
     */
    apply(cmd, { coalesce = true } = {}) {
      applyCommand(doc, cmd)
      redoStack.length = 0
      const now = Date.now()
      const prev = undoStack[undoStack.length - 1]
      if (coalesce && prev && now - lastAppliedAt < COALESCE_WINDOW_MS && canCoalesce(prev, cmd)) {
        coalesceInto(prev, cmd)
      } else {
        undoStack.push(cmd)
        if (undoStack.length > UNDO_LIMIT) {
          undoStack.shift()
        }
      }
      lastAppliedAt = now
      notify('command')
    },

    undo() {
      const cmd = undoStack.pop()
      if (!cmd) {
        return false
      }
      applyCommand(doc, invertCommand(cmd))
      redoStack.push(cmd)
      lastAppliedAt = 0
      notify('command')
      return true
    },

    redo() {
      const cmd = redoStack.pop()
      if (!cmd) {
        return false
      }
      applyCommand(doc, cmd)
      undoStack.push(cmd)
      lastAppliedAt = 0
      notify('command')
      return true
    },

    /**
     * 瞬态变更(SSE 落位/进度/错误):不进 undo 栈。
     * 变更函数直接操作 doc;返回值用于通知 payload。
     */
    /**
     * @param {(doc: CanvasDocData) => unknown} mutator
     */
    mutateTransient(mutator) {
      const payload = mutator(doc)
      notify('transient')
      return payload
    },
  }
}
