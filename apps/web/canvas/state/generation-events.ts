// SSE 客户端:订阅 /api/events,事件驱动 reducer;断线自动重连(EventSource 原生)
// 并在 open 时触发对账;首包 snapshot 携带进行中任务用于页面加载时的初次对账。
// 组任务(四方向,PRD §5.2)走整组语义:未终结只同步统一状态,全部终结才发布;
// 模块内维护 groupId → 任务 最新态缓冲,保证"整组是否终结"的判断有全量知识。

import {
  applyTransition,
  DISCONNECTED_MESSAGE,
  imageRecordToAsset,
  jobEventToTransition,
  markDisconnected,
  reconcile,
  type JobLike,
  type JobImageRecord,
  type ResolvedImage,
} from './generation-reducer.js'
import { applyGroupJobs, sweepLostGroupSlots } from './group-state.js'
import type { CanvasDocData } from './doc-store.js'
import type { GenerationCoordinator } from './generation-coordinator.js'
import { latestJob } from './job-version.js'

export type GenerationEventSource = {
  addEventListener(type: string, listener: (event: { data: string }) => void): void
  close(): void
  readyState?: number
}

export type GenerationEventsOptions = {
  /** 只依赖 mutateTransient:命令式变更经 reducer 应用,doc 形状由 DocStore 保证 */
  docStore: {
    mutateTransient: (fn: (doc: CanvasDocData) => unknown) => unknown
  }
  coordinator?: GenerationCoordinator
  onConnectionChange?: (connected: boolean) => void
  onReconciled?: (result: { changed: number; lost: number }) => void
  onGroupPublished?: (groupId: string) => void
  eventSourceFactory?: (url: string) => GenerationEventSource
  fetchImpl?: typeof fetch
}

export type GenerationEventsHandle = {
  reconcileNow(): Promise<void>
  close(): void
  readonly isConnected: boolean
}

export function connectGenerationEvents(options: GenerationEventsOptions): GenerationEventsHandle {
  const {
    docStore,
    coordinator,
    onConnectionChange = () => {},
    onReconciled = () => {},
    onGroupPublished = () => {},
    eventSourceFactory = (url) => new EventSource(url),
    fetchImpl = (...args) => fetch(...args),
  } = options

  const resolveImage = (imageRecord: JobImageRecord): ResolvedImage | null =>
    imageRecordToAsset(imageRecord)

  /** groupId → (jobId → 最新任务);重连对账时以服务端列表校准 */
  const groupBuffer = new Map<string, Map<string, JobLike>>()
  const versions = new Map<string, JobLike>()
  let polling: ReturnType<typeof setTimeout> | undefined
  let reconciling = false

  function bufferGroupJob(job: JobLike) {
    if (!job.groupId) {
      return
    }
    const bucket = groupBuffer.get(job.groupId) ?? new Map()
    bucket.set(job.id, job)
    groupBuffer.set(job.groupId, bucket)
  }

  /** 用缓冲中的整组任务驱动画布占位(统一状态 / 整组发布)。 */
  function applyGroupFromBuffer(groupId: string) {
    const bucket = groupBuffer.get(groupId)
    if (!bucket) {
      return
    }
    const result = docStore.mutateTransient((doc) =>
      applyGroupJobs(doc, [...bucket.values()], resolveImage),
    ) as { publishedGroups: string[]; changedObjIds: string[] }
    if (result.publishedGroups.includes(groupId) && result.changedObjIds.length > 0) {
      onGroupPublished(groupId)
    }
  }

  function handleJobEvent(job: JobLike) {
    if (latestJob(versions, job) !== job) {
      return
    }
    if (job.groupId) {
      bufferGroupJob(job)
      applyGroupFromBuffer(job.groupId)
      return
    }
    if (coordinator?.acceptJob(job)) {
      return
    }
    docStore.mutateTransient((doc) => {
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

  function findIdByJobId(doc: CanvasDocData, jobId: string): string | null {
    for (const obj of Object.values(doc.objects)) {
      if (obj.kind === 'placeholder' && obj.gen?.jobId === jobId) {
        return obj.id
      }
    }
    return null
  }

  /** 拉取服务端任务列表对账(页面加载 / 重连后) */
  async function reconcileNow() {
    if (closed || reconciling) {
      return
    }
    reconciling = true
    try {
      const res = await fetchImpl('/api/jobs?all=1&limit=100')
      if (res.ok === false || closed) {
        return
      }
      const body = (await res.json()) as { jobs?: JobLike[] }
      if (closed) {
        return
      }
      const jobs = (body.jobs || []).map((job) => latestJob(versions, job))
      for (const job of jobs) {
        if (!job.groupId) {
          coordinator?.acceptJob(job)
        }
      }
      // 组任务缓冲以服务端为准重建(重启/换页后本地缓冲为空)
      groupBuffer.clear()
      for (const job of jobs) {
        if (job.groupId) {
          bufferGroupJob(job)
        }
      }
      const result = docStore.mutateTransient((doc) => {
        const single = reconcile(
          doc,
          jobs.filter((j) => !j.groupId),
          resolveImage,
        )
        // 已终结且被服务端裁剪的组任务先兜底转错误卡,再整组应用现存任务
        const swept = sweepLostGroupSlots(doc, jobs)
        const group = applyGroupJobs(doc, jobs, resolveImage)
        return {
          changedIds: [...single.changedIds, ...swept.changedObjIds, ...group.changedObjIds],
          lostIds: single.lostIds,
          publishedGroups: group.publishedGroups,
        }
      }) as { changedIds: string[]; lostIds: string[]; publishedGroups: string[] }
      onReconciled({ changed: result.changedIds.length, lost: result.lostIds.length })
      for (const groupId of result.publishedGroups) {
        onGroupPublished(groupId)
      }
    } catch {
      // 对账失败不打断:下次重连再试
    } finally {
      reconciling = false
    }
  }

  let es: GenerationEventSource | null = null
  let closed = false
  let connected = false

  function scheduleFallback() {
    if (closed || connected || polling !== undefined) {
      return
    }
    polling = setTimeout(
      () => {
        polling = undefined
        void reconcileNow().finally(scheduleFallback)
      },
      4000 + Math.random() * 2000,
    )
  }

  function connect() {
    es = eventSourceFactory('/api/events')
    es.addEventListener('open', () => {
      if (closed) {
        return
      }
      connected = true
      clearTimeout(polling)
      polling = undefined
      onConnectionChange(true)
      // 重连成功即对账(S15 → S14)
      void reconcileNow()
    })
    es.addEventListener('snapshot', (e) => {
      if (closed) {
        return
      }
      connected = true
      onConnectionChange(true)
      try {
        const data = JSON.parse(e.data) as { jobs?: JobLike[] }
        const jobs = (data.jobs || []).map((job) => latestJob(versions, job))
        for (const job of jobs) {
          if (!job.groupId) {
            coordinator?.acceptJob(job)
          }
        }
        docStore.mutateTransient((doc) => {
          reconcile(
            doc,
            jobs.filter((j) => !j.groupId),
            resolveImage,
          )
          sweepLostGroupSlots(doc, jobs)
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
    es.addEventListener('job', (e) => {
      if (closed) {
        return
      }
      try {
        handleJobEvent(JSON.parse(e.data) as JobLike)
      } catch {
        // 事件体损坏:丢弃
      }
    })
    es.addEventListener('error', () => {
      if (closed) {
        return
      }
      connected = false
      scheduleFallback()
      // EventSource 自动重连;服务端 retry: 3000
      onConnectionChange(false)
      // 生成中的占位统一切到"连接中断,正在查询"(A05):不编造进度、不自动重提
      docStore.mutateTransient((doc) => markDisconnected(doc))
      for (const entry of coordinator?.runtime.values() || []) {
        if (entry.status === 'queued' || entry.status === 'running') {
          coordinator?.runtime.patch(entry.operationId, {
            status: 'disconnected',
            progress: undefined,
            message: DISCONNECTED_MESSAGE,
          })
        }
      }
    })
  }

  connect()

  return {
    reconcileNow,
    close() {
      closed = true
      clearTimeout(polling)
      es?.close()
    },
    get isConnected() {
      return closed ? false : es?.readyState === 1
    },
  }
}
