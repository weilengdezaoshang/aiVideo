// 自动保存(PRD §9 / S13):变更后 1s 防抖全量快照 POST;
// beforeunload 用 sendBeacon 冲刷;顶栏状态:保存中… → 已保存。
// 乐观并发(PRD §9 A13):快照带 baseRevision,服务端检测冲突返回 409,
// 客户端保留本机草稿并提示,不用最后写入静默覆盖。
// fetch/beacon 依赖注入,便于 node:test。

// 本机草稿(PRD §9/A13):每次冲刷前先落 localStorage——服务端写失败/进程崩溃时
// 最多丢最近一次防抖窗口内的修改,且可在"服务端读取失败"时恢复;
// 保存确认成功后草稿使命完成,予以清除(未确认的草稿一律保留)。
import { clearLocalDraft, writeLocalDraft } from './document-api.js'
import type { CanvasDocData } from './doc-store.js'

const DEBOUNCE_MS = 1000

export type SaveStatus = 'idle' | 'saving' | 'saved' | 'error' | 'conflict'

export type AutoSaverOptions = {
  docId: string
  getDoc: () => CanvasDocData
  fetchImpl?: typeof fetch
  beaconImpl?: (url: string, body: Blob) => boolean
  onStatus?: (status: SaveStatus, detail?: string) => void
  debounceMs?: number
}

export type AutoSaver = {
  markDirty(): void
  flush(options?: { force?: boolean }): Promise<void>
  forceSave(): Promise<void>
  ensureSaved(): Promise<void>
  readonly inConflict: boolean
  clearConflict(): void
  flushOnUnload(): void
}

export function createAutoSaver(options: AutoSaverOptions): AutoSaver {
  const {
    docId,
    getDoc,
    fetchImpl = (...args) => fetch(...args),
    beaconImpl = (url, body) => navigator.sendBeacon(url, body),
    onStatus = () => {},
    debounceMs = DEBOUNCE_MS,
  } = options

  const url = `/api/documents/${docId}`
  let timer: ReturnType<typeof setTimeout> | null = null
  let saving = false
  let pendingAgain = false
  let inConflict = false
  let lastSaveError: Error | null = null
  const idleWaiters: (() => void)[] = []

  /** 保存成功后静默同步服务端 revision(不触发订阅者,避免保存风暴) */
  function syncRevision(document: { revision?: number } | undefined) {
    if (document && typeof document.revision === 'number') {
      getDoc().revision = document.revision
    }
  }

  async function flush({ force = false } = {}): Promise<void> {
    if (saving) {
      pendingAgain = true
      return
    }
    saving = true
    const snapshot = JSON.parse(
      JSON.stringify({
        ...getDoc(),
        storyboard: getDoc().storyboard ?? null,
        timeline: getDoc().timeline ?? null,
      }),
    ) as CanvasDocData & {
      baseRevision?: number
    }
    if (!force) {
      snapshot.baseRevision = snapshot.revision
    }
    // 服务端写入前先落本机草稿(写失败只影响兜底,不阻塞保存)
    writeLocalDraft(docId, snapshot)
    try {
      const res = await fetchImpl(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(snapshot),
      })
      if (res.status === 409) {
        // 冲突:保留本机草稿,通知 UI;不再自动重试(等用户决定)
        inConflict = true
        const body = (await res.json().catch(() => ({}))) as { error?: string }
        lastSaveError = new Error(body.error || '文档已被其他会话修改')
        onStatus('conflict', lastSaveError.message)
        return
      }
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { error?: string }
        throw new Error(body.error || `HTTP ${res.status}`)
      }
      inConflict = false
      syncRevision(((await res.json()) as { document?: { revision?: number } }).document)
      // 保存确认:服务端已持久化本次快照,本机草稿不再是唯一副本
      clearLocalDraft(docId)
      lastSaveError = null
      onStatus('saved')
    } catch (err) {
      lastSaveError = err instanceof Error ? err : new Error(String(err) || '保存失败')
      onStatus('error', lastSaveError.message)
    } finally {
      saving = false
      for (const resolve of idleWaiters.splice(0)) {
        resolve()
      }
      if (pendingAgain) {
        pendingAgain = false
        void flush()
      }
    }
  }

  return {
    /** 文档变更时调用:进入防抖;立即反馈"保存中" */
    markDirty() {
      onStatus(inConflict ? 'conflict' : 'saving')
      if (timer) {
        clearTimeout(timer)
      }
      timer = setTimeout(() => {
        timer = null
        void flush()
      }, debounceMs)
    },
    flush,
    /** 服务端操作前等待已排队保存，再确认最新文档保存成功；不强制覆盖冲突。 */
    async ensureSaved() {
      if (timer) {
        clearTimeout(timer)
        timer = null
      }
      while (saving) {
        await new Promise<void>((resolve) => idleWaiters.push(resolve))
      }
      if (inConflict) {
        throw lastSaveError || new Error('请先解决文档保存冲突')
      }
      await flush()
      while (saving) {
        await new Promise<void>((resolve) => idleWaiters.push(resolve))
      }
      if (lastSaveError) {
        throw lastSaveError
      }
    },
    /** 用户确认后可强制覆盖一次(不带 baseRevision);常规 UI 不暴露此入口 */
    forceSave() {
      inConflict = false
      return flush({ force: true })
    },
    get inConflict() {
      return inConflict
    },
    clearConflict() {
      inConflict = false
    },
    /** 页面即将卸载:beacon 同步冲刷(不保证送达,配合服务端防抖窗口) */
    flushOnUnload() {
      if (timer) {
        clearTimeout(timer)
        timer = null
      }
      try {
        const snapshot = JSON.parse(
          JSON.stringify({
            ...getDoc(),
            storyboard: getDoc().storyboard ?? null,
            timeline: getDoc().timeline ?? null,
          }),
        ) as CanvasDocData & {
          baseRevision?: number
        }
        snapshot.baseRevision = snapshot.revision
        writeLocalDraft(docId, snapshot)
        const ok = beaconImpl(
          url,
          new Blob([JSON.stringify(snapshot)], { type: 'application/json' }),
        )
        if (!ok) {
          // beacon 排队失败(极少见):退回 keepalive fetch
          void fetchImpl(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(snapshot),
            keepalive: true,
          })
        }
      } catch {
        // 忽略序列化异常:文档始终为可序列化 JSON
      }
    },
  }
}
