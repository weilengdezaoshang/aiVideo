import {
  applyCommand,
  canCoalesce,
  coalesceInto,
  invertCommand,
  type CanvasCommand,
  type CommandDoc,
} from './commands.js'

const COALESCE_WINDOW_MS = 600
const DEFAULT_LIMIT = 100

export type HistoryChange = {
  action: 'apply' | 'undo' | 'redo'
  command: CanvasCommand
}

export type HistoryManager = {
  canUndo(): boolean
  canRedo(): boolean
  apply(command: CanvasCommand, options?: { coalesce?: boolean }): void
  undo(): boolean
  redo(): boolean
  subscribe(listener: (change: HistoryChange) => void): () => boolean
}

export function createHistoryManager(
  doc: CommandDoc,
  options: { limit?: number; onDocumentChange?: () => void } = {},
): HistoryManager {
  const undoStack: CanvasCommand[] = []
  const redoStack: CanvasCommand[] = []
  const listeners = new Set<(change: HistoryChange) => void>()
  const limit = options.limit ?? DEFAULT_LIMIT
  let lastAppliedAt = 0

  const emit = (action: HistoryChange['action'], command: CanvasCommand) => {
    options.onDocumentChange?.()
    for (const listener of listeners) {
      listener({ action, command })
    }
  }

  return {
    canUndo: () => undoStack.length > 0,
    canRedo: () => redoStack.length > 0,
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    apply(command, { coalesce = true } = {}) {
      applyCommand(doc, command)
      redoStack.length = 0
      const now = Date.now()
      const previous = undoStack[undoStack.length - 1]
      if (
        coalesce &&
        previous &&
        now - lastAppliedAt < COALESCE_WINDOW_MS &&
        canCoalesce(previous, command)
      ) {
        coalesceInto(previous, command)
      } else {
        undoStack.push(command)
        if (undoStack.length > limit) {
          undoStack.shift()
        }
      }
      lastAppliedAt = now
      emit('apply', command)
    },
    undo() {
      const command = undoStack.pop()
      if (!command) {
        return false
      }
      applyCommand(doc, invertCommand(command))
      redoStack.push(command)
      lastAppliedAt = 0
      emit('undo', command)
      return true
    },
    redo() {
      const command = redoStack.pop()
      if (!command) {
        return false
      }
      applyCommand(doc, command)
      undoStack.push(command)
      lastAppliedAt = 0
      emit('redo', command)
      return true
    },
  }
}
