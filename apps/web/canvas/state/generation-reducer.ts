// 生成事件 reducer(纯函数,可 node:test):SSE 的 job/image 事件 + 对账快照 → 文档变更指令。
// 核心语义(PRD §8.2):
//   - 任务绑定对象 id(clientRef = 占位对象 id),不绑坐标;
//   - 未知 jobId 一律安全丢弃(防全局广播/事件先于 POST 响应);
//   - reducer 只产出数据(变更描述),由调用方应用到 DocStore。

import type { CanvasObj } from './commands.js'

/** 服务端任务记录里画布关心的字段(job/image 事件与对账快照共用) */
export type JobImageRecord = {
  id?: string
  url?: string
  file?: string
  params?: { kind?: string; width?: number; height?: number }
}

export type JobLike = {
  id: string
  status: string
  progress?: number
  message?: string
  error?: string
  images?: JobImageRecord[]
  groupId?: string
  slot?: number
  directionTitle?: string
  clientRef?: string
  stateVersion?: number
  progressSeq?: number
  code?: string | null
  recovery?: string | null
  retryAfter?: number | null
}

/** job 事件计算出的占位变更描述 */
export type JobTransition =
  | { kind: 'none' }
  | { kind: 'progress'; jobId: string; progress?: number; message: string }
  | { kind: 'complete'; image: JobImageRecord }
  | { kind: 'fail'; error: string }

/** 完成记录解析后的落位字段(resolveImage 的返回) */
export type ResolvedImage = {
  kind: 'image' | 'video'
  assetId: string | null
  src: string
  ext: string
  width?: number
  height?: number
  hasThumbs: boolean
  imageId?: string
}

export type ResolveImage = (image: JobImageRecord) => ResolvedImage | null

/** 从 SSE job 事件计算占位对象应变成什么。 */
export function jobEventToTransition(obj: CanvasObj | undefined, job: JobLike): JobTransition {
  if (!obj || obj.kind !== 'placeholder') {
    return { kind: 'none' }
  }
  if (job.status === 'running' || job.status === 'queued') {
    return {
      kind: 'progress',
      // 事件先于 POST 响应到达时占位 gen.jobId 为空,用事件自带的 id 回填(PRD §8.4-4)
      jobId: job.id,
      progress: typeof job.progress === 'number' ? job.progress : 0,
      message: job.message || (job.status === 'queued' ? '排队中' : '生成中'),
    }
  }
  if (job.status === 'completed') {
    const image = job.images?.[job.images.length - 1]
    if (!image) {
      return { kind: 'fail', error: '任务完成但缺少产物' }
    }
    return { kind: 'complete', image }
  }
  if (job.status === 'failed') {
    return { kind: 'fail', error: job.error || '生成失败' }
  }
  return { kind: 'none' }
}

/** 应用 transition 到文档(mutateTransient 用;返回是否发生变化)。 */
export function applyTransition(
  doc: { objects: Record<string, CanvasObj> },
  objId: string,
  transition: JobTransition,
  resolveImage?: ResolveImage,
): boolean {
  const obj = doc.objects[objId]
  if (!obj) {
    return false
  }
  switch (transition.kind) {
    case 'progress': {
      obj.gen = {
        ...(obj.gen || {}),
        jobId: obj.gen?.jobId || transition.jobId || '',
        progress: transition.progress,
        message: transition.message,
      }
      return true
    }
    case 'fail': {
      obj.kind = 'error'
      obj.errorDetail = transition.error
      delete obj.gen
      obj.height = Math.max(120, Math.min(obj.height, 160))
      return true
    }
    case 'complete': {
      const resolved = resolveImage?.(transition.image)
      if (!resolved) {
        return false
      }
      obj.kind = resolved.kind || 'image'
      obj.assetId = resolved.assetId || undefined
      obj.src = resolved.src
      obj.imageId = resolved.imageId || undefined
      obj.hasThumbs = resolved.hasThumbs !== false
      obj.ext = resolved.ext
      obj.width = resolved.width || obj.width
      obj.height = resolved.height || obj.height
      delete obj.gen
      return true
    }
    default:
      return false
  }
}

/**
 * 对账(reconcile):以服务端任务列表为准,校正本地占位。
 * 规则(PRD §8.2 + S14/S16):
 *   - 服务端仍在跑(running/queued)→ 更新进度;
 *   - 服务端已完成 → 落位;
 *   - 服务端失败(含重启中断)→ 错误卡(可重试);
 *   - 本地占位的 jobId 服务端查无 → 任务丢失错误卡;
 *   - 本地非占位对象不动。
 */
export function reconcile(
  doc: { objects: Record<string, CanvasObj> },
  serverJobs: JobLike[],
  resolveImage?: ResolveImage,
): { changedIds: string[]; lostIds: string[] } {
  const byJobId = new Map(serverJobs.map((j) => [j.id, j]))
  const changedIds: string[] = []
  const lostIds: string[] = []
  for (const obj of Object.values(doc.objects)) {
    if (obj.kind !== 'placeholder' || !obj.gen?.jobId) {
      continue
    }
    if (obj.groupId) {
      // 组槽位由 group-state 整组语义管理(reconcile 的单任务查无即丢会误伤:
      // 传入的 serverJobs 已过滤掉组任务,组槽位在这里必然"查无")
      continue
    }
    const jobId = obj.gen.jobId
    const job = byJobId.get(jobId)
    if (!job) {
      obj.kind = 'error'
      obj.errorDetail = '任务丢失:服务已重启,该任务未能找回'
      delete obj.gen
      obj.height = Math.max(120, Math.min(obj.height, 160))
      changedIds.push(obj.id)
      lostIds.push(obj.id)
      continue
    }
    const transition = jobEventToTransition(obj, job)
    if (transition.kind !== 'none') {
      if (applyTransition(doc, obj.id, transition, resolveImage)) {
        changedIds.push(obj.id)
      }
    }
  }
  return { changedIds, lostIds }
}

/**
 * SSE 断连(PRD §5.2 A05):把所有绑定任务的占位统一改为
 * "连接中断,正在查询"——不编造进度、不自动重提;重连对账后由事件恢复常态文案。
 * @returns 被更新的占位 id
 */
export function markDisconnected(doc: { objects: Record<string, CanvasObj> }): string[] {
  const changed: string[] = []
  for (const obj of Object.values(doc.objects)) {
    if (obj.kind !== 'placeholder' || !obj.gen?.jobId) {
      continue
    }
    // 本地任务(如抠图合成)不走 SSE,断线不影响其执行,不改写文案
    if (String(obj.gen.jobId).startsWith('local-')) {
      continue
    }
    if (obj.gen.message !== DISCONNECTED_MESSAGE) {
      obj.gen = { ...obj.gen, progress: undefined, message: DISCONNECTED_MESSAGE }
      changed.push(obj.id)
    }
  }
  return changed
}

export const DISCONNECTED_MESSAGE = '连接中断,正在查询'

/**
 * 从完成的 image 记录(服务端 ImageRecord)解析出对象所需字段。
 * 画布产物直接复用旧 images 存储;assetId 为空时用 url 直连。
 */
export function imageRecordToAsset(imageRecord: JobImageRecord | undefined): ResolvedImage | null {
  if (!imageRecord?.url) {
    return null
  }
  return {
    kind: imageRecord.params?.kind === 'video' ? 'video' : 'image',
    assetId: null,
    src: imageRecord.url,
    ext: (imageRecord.file || '').split('.').pop() || 'png',
    width: imageRecord.params?.width,
    height: imageRecord.params?.height,
    hasThumbs: false,
    imageId: imageRecord.id,
  }
}
