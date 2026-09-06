// SSE 客户端:订阅 /api/events,事件驱动 reducer;断线自动重连(EventSource 原生)
// 并在 open 时触发对账;首包 snapshot 携带进行中任务用于页面加载时的初次对账。

import {
  applyTransition,
  imageRecordToAsset,
  jobEventToTransition,
  reconcile,
} from './generation-reducer.js'

/**
 * @param {{
 *   docStore: { doc: any, mutateTransient: (fn: (doc: any) => unknown) => unknown },
 *   onConnectionChange?: (connected: boolean) => void,
 *   onReconciled?: (result: { changed: number, lost: number }) => void,
 *   eventSourceFactory?: (url: string) => { addEventListener: Function, close: Function },
 *   fetchImpl?: typeof fetch,
 * }} options
 */
export function connectGenerationEvents(options) {
  const {
    docStore,
    onConnectionChange = () => {},
    onReconciled = () => {},
    eventSourceFactory = (url) => new EventSource(url),
    fetchImpl = (...args) => fetch(...args),
  } = options

  /** @param {any} imageRecord */
  const resolveImage = (imageRecord) => imageRecordToAsset(imageRecord)

  /** @param {any} job */
  function handleJobEvent(job) {
    docStore.mutateTransient((doc) => {
      // clientRef = 占位对象 id;事件先于 POST 响应时靠它对齐(PRD §8.4-4)
      const objId = job.clientRef || findIdByJobId(doc, job.id)
      if (!objId) {
        return false // 未知 jobId:安全丢弃
      }
      const obj = doc.objects[objId]
      const transition = jobEventToTransition(obj, job)
      if (transition.kind === 'none') {
        return false
      }
      return applyTransition(doc, objId, transition, resolveImage)
    })
  }

  /** @param {any} doc @param {string} jobId */
  function findIdByJobId(doc, jobId) {
    for (const obj of Object.values(doc.objects)) {
      if (obj.kind === 'placeholder' && obj.gen?.jobId === jobId) {
        return obj.id
      }
    }
    return null
  }

  /** 拉取服务端任务列表对账(页面加载 / 重连后) */
  async function reconcileNow() {
    try {
      const res = await fetchImpl('/api/jobs?all=1&limit=100')
      const body = await res.json()
      /** @type {{ changedIds: string[], lostIds: string[] }} */
      const result = /** @type {any} */ (
        docStore.mutateTransient((/** @type {any} */ doc) =>
          reconcile(doc, body.jobs || [], resolveImage),
        )
      )
      onReconciled({ changed: result.changedIds.length, lost: result.lostIds.length })
    } catch {
      // 对账失败不打断:下次重连再试
    }
  }

  /** @type {any} */
  let es = null
  let closed = false

  function connect() {
    es = eventSourceFactory('/api/events')
    es.addEventListener('open', () => {
      onConnectionChange(true)
      // 重连成功即对账(S15 → S14)
      void reconcileNow()
    })
    es.addEventListener('snapshot', (/** @type {{ data: string }} */ e) => {
      onConnectionChange(true)
      try {
        const data = JSON.parse(e.data)
        docStore.mutateTransient((doc) => reconcile(doc, data.jobs || [], resolveImage))
      } catch {
        // 快照损坏:忽略,等下一个事件
      }
    })
    es.addEventListener('job', (/** @type {{ data: string }} */ e) => {
      try {
        handleJobEvent(JSON.parse(e.data))
      } catch {
        // 事件体损坏:丢弃
      }
    })
    es.addEventListener('error', () => {
      // EventSource 自动重连;服务端 retry: 3000
      onConnectionChange(false)
    })
  }

  connect()

  return {
    reconcileNow,
    close() {
      closed = true
      es?.close()
    },
    get isConnected() {
      return closed ? false : es?.readyState === 1
    },
  }
}
