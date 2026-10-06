import { clearLocalDraftIfMatches, draftMatchesDocument, writeLocalDraft } from './document-api.js'
import type { CanvasDocData } from './doc-store.js'

export type SaveStatus = 'idle' | 'saving' | 'saved' | 'error' | 'conflict'
export type AutoSaverOptions = {
  docId: string
  getDoc: () => CanvasDocData
  fetchImpl?: typeof fetch
  onStatus?: (status: SaveStatus, detail?: string) => void
  debounceMs?: number
}
export type AutoSaver = {
  markDirty(): void
  flush(options?: { force?: boolean }): Promise<void>
  forceSave(): Promise<void>
  ensureSaved(): Promise<number>
  readonly inConflict: boolean
  clearConflict(): void
  flushOnUnload(): void
}

/** One revision-ordered queue, including unload. Unacknowledged edits retain a draft. */
export function createAutoSaver(options: AutoSaverOptions): AutoSaver {
  const {
    docId,
    getDoc,
    fetchImpl = (...args) => fetch(...args),
    onStatus = () => {},
    debounceMs = 1000,
  } = options
  const url = `/api/documents/${encodeURIComponent(docId)}`
  const snapshot = () =>
    JSON.parse(
      JSON.stringify({
        ...getDoc(),
        storyboard: getDoc().storyboard ?? null,
        timeline: getDoc().timeline ?? null,
      }),
    ) as CanvasDocData
  let acknowledged = snapshot()
  let timer: ReturnType<typeof setTimeout> | undefined
  let running: Promise<void> | undefined
  let requested = false
  let dirty = false
  let unloading = false
  let inConflict = false
  let lastSaveError: Error | undefined
  const clearTimer = () => {
    clearTimeout(timer)
    timer = undefined
  }

  async function drain(): Promise<void> {
    while (requested && !inConflict) {
      requested = false
      clearTimer()
      const current = snapshot()
      writeLocalDraft(docId, current)
      onStatus('saving')
      try {
        const res = await fetchImpl(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ...current, baseRevision: current.revision }),
          ...(unloading ? { keepalive: true } : {}),
        })
        const body = (await res.json().catch(() => ({}))) as {
          error?: string
          document?: { revision?: number }
        }
        if (!res.ok) {
          inConflict = res.status === 409
          throw new Error(body.error || `保存失败 HTTP ${res.status}`)
        }
        if (typeof body.document?.revision === 'number') {
          getDoc().revision = body.document.revision
        }
        acknowledged = current
        lastSaveError = undefined
        // A newer edit may have replaced the draft while this POST was in flight.
        clearLocalDraftIfMatches(docId, current)
        dirty = !draftMatchesDocument(getDoc(), current)
        if (dirty) {
          requested = true
        }
        onStatus(dirty ? 'saving' : 'saved')
      } catch (cause) {
        lastSaveError = cause instanceof Error ? cause : new Error(String(cause))
        onStatus(inConflict ? 'conflict' : 'error', lastSaveError.message)
        requested = false
        break
      }
    }
  }

  function flush(): Promise<void> {
    clearTimer()
    if (inConflict) {
      return Promise.resolve()
    }
    requested = true
    if (!running) {
      running = drain().finally(() => {
        running = undefined
        if (requested && !inConflict) {
          void flush()
        }
      })
    }
    return running
  }

  return {
    markDirty() {
      dirty = true
      writeLocalDraft(docId, snapshot())
      onStatus(inConflict ? 'conflict' : 'saving')
      clearTimer()
      if (inConflict) {
        return
      }
      if (running) {
        requested = true
      } else {
        timer = setTimeout(() => {
          void flush()
        }, debounceMs)
      }
    },
    flush,
    async ensureSaved() {
      clearTimer()
      if (running) {
        await running
      }
      if (!inConflict && (dirty || !draftMatchesDocument(acknowledged, getDoc()))) {
        await flush()
      }
      if (lastSaveError || inConflict) {
        throw lastSaveError || new Error('请先解决文档保存冲突')
      }
      const revision = getDoc().revision
      if (!Number.isInteger(revision) || !revision || revision < 1) {
        throw new Error('文档尚无已确认版本，请先保存画布')
      }
      return revision
    },
    // Explicit replacement still uses CAS: an edit after this read must conflict.
    async forceSave() {
      if (running) {
        await running
      }
      const response = await fetchImpl(url)
      if (!response.ok) {
        throw new Error('无法读取最新文档版本')
      }
      const body = (await response.json()) as { document: CanvasDocData }
      getDoc().revision = body.document.revision
      inConflict = false
      await flush()
    },
    get inConflict() {
      return inConflict
    },
    clearConflict() {
      inConflict = false
    },
    flushOnUnload() {
      clearTimer()
      if (!dirty && draftMatchesDocument(acknowledged, getDoc())) {
        return
      }
      writeLocalDraft(docId, snapshot())
      unloading = true
      // Never race the preceding POST using its baseRevision. If the page dies before
      // its acknowledgement, the latest draft survives for explicit recovery on open.
      void flush()
    },
  }
}
