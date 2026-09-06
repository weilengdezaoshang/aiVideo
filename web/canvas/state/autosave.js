// 自动保存(PRD §5.3-⑤ / S13):变更后 2s 防抖全量快照 POST;
// beforeunload 用 sendBeacon 冲刷;顶栏状态:保存中… → 已保存。
// fetch/beacon 依赖注入,便于 node:test。

const DEBOUNCE_MS = 2000

/**
 * @param {{
 *   docId: string,
 *   getDoc: () => object,
 *   fetchImpl?: typeof fetch,
 *   beaconImpl?: (url: string, body: Blob) => boolean,
 *   onStatus?: (status: 'idle' | 'saving' | 'saved' | 'error', detail?: string) => void,
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

  async function flush() {
    if (saving) {
      pendingAgain = true
      return
    }
    saving = true
    const snapshot = JSON.parse(JSON.stringify(getDoc()))
    try {
      const res = await fetchImpl(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(snapshot),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.error || `HTTP ${res.status}`)
      }
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
      onStatus('saving')
      if (timer) {
        clearTimeout(timer)
      }
      timer = setTimeout(() => {
        timer = null
        void flush()
      }, debounceMs)
    },
    flush,
    /** 页面即将卸载:beacon 同步冲刷(不保证送达,配合服务端 2s 窗口) */
    flushOnUnload() {
      if (timer) {
        clearTimeout(timer)
        timer = null
      }
      try {
        const ok = beaconImpl(
          url,
          new Blob([JSON.stringify(getDoc())], { type: 'application/json' }),
        )
        if (!ok) {
          // beacon 排队失败(极少见):退回 keepalive fetch
          void fetchImpl(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(getDoc()),
            keepalive: true,
          })
        }
      } catch {
        // 忽略序列化异常:文档始终为可序列化 JSON
      }
    },
  }
}
