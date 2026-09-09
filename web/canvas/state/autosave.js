// 自动保存(PRD §9 / S13):变更后 1s 防抖全量快照 POST;
// beforeunload 用 sendBeacon 冲刷;顶栏状态:保存中… → 已保存。
// 乐观并发(PRD §9 A13):快照带 baseRevision,服务端检测冲突返回 409,
// 客户端保留本机草稿并提示,不用最后写入静默覆盖。
// fetch/beacon 依赖注入,便于 node:test。

const DEBOUNCE_MS = 1000

/**
 * @param {{
 *   docId: string,
 *   getDoc: () => any,
 *   fetchImpl?: typeof fetch,
 *   beaconImpl?: (url: string, body: Blob) => boolean,
 *   onStatus?: (status: 'idle' | 'saving' | 'saved' | 'error' | 'conflict', detail?: string) => void,
 *   debounceMs?: number,
 * }} options
 */
export function createAutoSaver(options) {
  const {
    docId,
    getDoc,
    fetchImpl = (...args) => fetch(...args),
    beaconImpl = (url, body) => navigator.sendBeacon(url, body),
    onStatus = () => {},
    debounceMs = DEBOUNCE_MS,
  } = options

  const url = `/api/documents/${docId}`
  /** @type {ReturnType<typeof setTimeout> | null} */
  let timer = null
  let saving = false
  let pendingAgain = false
  let inConflict = false

  /** 保存成功后静默同步服务端 revision(不触发订阅者,避免保存风暴) */
  /** @param {{ revision?: number }} document */
  function syncRevision(document) {
    if (document && typeof document.revision === 'number') {
      getDoc().revision = document.revision
    }
  }

  async function flush({ force = false } = {}) {
    if (saving) {
      pendingAgain = true
      return
    }
    saving = true
    const snapshot = JSON.parse(JSON.stringify(getDoc()))
    if (!force) {
      snapshot.baseRevision = snapshot.revision
    }
    try {
      const res = await fetchImpl(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(snapshot),
      })
      if (res.status === 409) {
        // 冲突:保留本机草稿,通知 UI;不再自动重试(等用户决定)
        inConflict = true
        const body = await res.json().catch(() => ({}))
        onStatus('conflict', body.error || '文档已被其他会话修改')
        return
      }
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.error || `HTTP ${res.status}`)
      }
      inConflict = false
      syncRevision((await res.json()).document)
      onStatus('saved')
    } catch (err) {
      onStatus('error', (err instanceof Error ? err.message : String(err)) || '保存失败')
    } finally {
      saving = false
      if (pendingAgain) {
        pendingAgain = false
        void flush()
      }
    }
  }

  return {
    /** 文档变更时调用:进入防抖;立即反馈"保存中" */
    markDirty() {
      if (inConflict) {
        // 冲突未解决前不接受新的本地提交提示,但继续防抖排队内容变更
      }
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
        const snapshot = JSON.parse(JSON.stringify(getDoc()))
        snapshot.baseRevision = snapshot.revision
        const ok = beaconImpl(url, new Blob([JSON.stringify(snapshot)], { type: 'application/json' }))
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
