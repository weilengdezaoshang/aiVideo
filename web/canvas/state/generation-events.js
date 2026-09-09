// SSE 客户端:订阅 /api/events,事件驱动 reducer;断线自动重连(EventSource 原生)
// 并在 open 时触发对账;首包 snapshot 携带进行中任务用于页面加载时的初次对账。 
// 组任务(四方向,PRD §5.2)走整组语义:未终结只同步统一状态,全部终结才发布;
// 模块内维护 groupId → 任务 最新态缓冲,保证"整组是否终结"的判断有全量知识。 

import {
  applyTransition,
  imageRecordToAsset,
  jobEventToTransition,
  reconcile,
} from './generation-reducer.js'
import { applyGroupJobs } from './group-state.js'

/**
 * @param {{
 *   docStore: { doc: any, mutateTransient: (fn: (doc: any) => unknown) => unknown },
 *   onConnectionChange?: (connected: boolean) => void,
 *   onReconciled?: (result: { changed: number, lost: number }) => void,
 *   onGroupPublished?: (groupId: string) => void,
 *   eventSourceFactory?: (url: string) => { addEventListener: Function, close: Function },
 *   fetchImpl?: typeof fetch,
 * }} options
 */
export function connectGenerationEvents(options) {
  const {
    docStore,
    onConnectionChange = () => {},
    onReconciled = () => {},
    onGroupPublished = () => {},
    eventSourceFactory = (url) => new EventSource(url),
    fetchImpl = (...args) => fetch(...args),
  } = options

  /** @param {any} imageRecord */
  const resolveImage = (imageRecord) => imageRecordToAsset(imageRecord)

  /** groupId → (jobId → 最新任务);重连对账时以服务端列表校准 */
  const groupBuffer = new Map()

  /** @param {any} job */
  function bufferGroupJob(job) {
    const bucket = groupBuffer.get(job.groupId) ?? new Map()
    bucket.set(job.id, job)
    groupBuffer.set(job.groupId, bucket)
  }

  /** 用缓冲中的整组任务驱动画布占位(统一状态 / 整组发布)。 @param {string} groupId */
  function applyGroupFromBuffer(groupId) {
    const bucket = groupBuffer.get(groupId)
    if (!bucket) {
      return
    }
    const result = /** @type {any} */ (
      docStore.mutateTransient((/** @type {any} */ doc) =>
        applyGroupJobs(doc, [...bucket.values()], resolveImage),
      )
    )
    if (result.publishedGroups.includes(groupId) && result.changedObjIds.length > 0) {
      onGroupPublished(groupId)
    }
  }

  /** @param {any} job */
  function handleJobEvent(job) {
    if (job.groupId) {
      bufferGroupJob(job)
      applyGroupFromBuffer(job.groupId)
      return
    }
    docStore.mutateTransient((/** @type {any} */ doc) => {
      // 单任务:clientRef = 占位对象 id;事件先于 POST 响应时靠它对齐(PRD §8.4-4)
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
      const jobs = body.jobs || []
      // 组任务缓冲以服务端为准重建(重启/换页后本地缓冲为空)
      groupBuffer.clear()
      for (const job of jobs) {
        if (job.groupId) {
          bufferGroupJob(job)
        }
      }
      const result = /** @type {any} */ (
        docStore.mutateTransient((/** @type {any} */ doc) => {
          const single = reconcile(doc, jobs.filter((/** @type {any} */ j) => !j.groupId), resolveImage)
          const group = applyGroupJobs(doc, jobs, resolveImage)
          return {
            changedIds: [...single.changedIds, ...group.changedObjIds],
            lostIds: single.lostIds,
            publishedGroups: group.publishedGroups,
          }
        })
      )
      onReconciled({ changed: result.changedIds.length, lost: result.lostIds.length })
      for (const groupId of result.publishedGroups) {
        onGroupPublished(groupId)
      }
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
        const jobs = data.jobs || []
        docStore.mutateTransient((/** @type {any} */ doc) => {
          reconcile(doc, jobs.filter((/** @type {any} */ j) => !j.groupId), resolveImage)
          applyGroupJobs(doc, jobs, resolveImage)
        })
        // 首包也校准组缓冲(不触发发布回调:发布走 reconcileNow 的全量路径)
        for (const job of jobs) {
          if (job.groupId) {
            bufferGroupJob(job)
          }
        }
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
