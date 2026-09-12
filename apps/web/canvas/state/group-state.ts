// 四方向组状态(纯函数,可 node:test;PRD §5.2):
//   - 组 = 同一 groupId 的任务集合,slot 0..3 横排固定;
//   - 未全部终结 → 四格统一状态文案(准备中 / 生成中…),不逐张显露进度;
//   - 全部终结 → 一次性产出全部槽位的落位/失败 transition(整组发布),
//     已完成资产由服务端持久化,但前端共同呈现,不虚构 1/4 完成。
// 占位对象 id = job.clientRef(与单图任务同约定);对象已被用户删除的槽位跳过,不复活。

import type { CanvasObj } from './commands.js'
import type { JobLike, JobTransition, ResolveImage } from './generation-reducer.js'

export type { JobLike }

/** 按 groupId 分桶(保持 slot 排序)。 */
export function groupJobs(jobs: JobLike[]): Map<string, JobLike[]> {
  const map = new Map<string, JobLike[]>()
  for (const job of jobs) {
    if (!job.groupId) {
      continue
    }
    const list = map.get(job.groupId) ?? []
    list.push(job)
    map.set(job.groupId, list)
  }
  for (const list of map.values()) {
    list.sort((a, b) => (a.slot ?? 0) - (b.slot ?? 0))
  }
  return map
}

/** @returns 组内任务是否全部终结 */
export function isGroupTerminal(slots: JobLike[]): boolean {
  return slots.every((j) => j.status === 'completed' || j.status === 'failed')
}

/** 组的统一状态文案:全部排队 = 准备中;任一开始执行 = 生成中…(PRD §5.2)。 */
export function groupUnifiedMessage(slots: JobLike[]): string {
  if (slots.some((j) => j.status === 'running' || j.status === 'completed')) {
    return '生成中…'
  }
  return '准备中'
}

/** 组整体状态(对话卡展示)。 */
export function groupCardState(slots: JobLike[]): { key: string; label: string } {
  if (!isGroupTerminal(slots)) {
    return { key: 'running', label: groupUnifiedMessage(slots) }
  }
  const succeeded = slots.filter((j) => j.status === 'completed').length
  if (succeeded === 0) {
    return { key: 'failed', label: '生成失败' }
  }
  if (succeeded < slots.length) {
    return { key: 'partial', label: `${succeeded}/${slots.length} 已生成` }
  }
  return { key: 'done', label: `${succeeded}/${slots.length} 已生成` }
}

export type GroupTransition = { objId: string; transition: JobTransition }

/**
 * 计算一组任务当前应对画布占位应用的 transition 列表。
 * 未终结 → 每个槽位一条统一 progress;全部终结 → 每槽位一条 complete/fail。
 * complete 携带原始 image 记录,由调用方(applyGroupJobs)解析为资产字段。
 * @param slots 同组任务(≥1)
 */
export function groupTransitions(
  doc: { objects: Record<string, CanvasObj> },
  slots: JobLike[],
  groupId: string,
): { groupId: string; publishes: boolean; transitions: GroupTransition[] } {
  const terminal = isGroupTerminal(slots)
  const message = groupUnifiedMessage(slots)
  const transitions: GroupTransition[] = []
  for (const job of slots) {
    const objId = job.clientRef
    const obj = objId ? doc.objects[objId] : null
    // 槽位以唯一 clientRef(= 占位对象 id)对齐;本地 groupId 与服务端 groupId
    // 各自生成,不做相等性比较(客户端本地占位先于服务端任务创建)
    if (!obj || obj.kind !== 'placeholder' || !objId) {
      continue // 槽位对象不存在或已被替换:跳过,不复活
    }
    if (!terminal) {
      transitions.push({
        objId,
        transition: {
          kind: 'progress',
          jobId: job.id,
          progress: undefined, // 组内不显示百分比(不虚构进度)
          message,
        },
      })
      continue
    }
    if (job.status === 'completed') {
      const image = job.images?.[job.images.length - 1]
      transitions.push({
        objId,
        transition: image
          ? ({ kind: 'complete', image } satisfies JobTransition)
          : ({ kind: 'fail', error: '任务完成但缺少产物' } satisfies JobTransition),
      })
    } else {
      transitions.push({
        objId,
        transition: { kind: 'fail', error: job.error || '生成失败' },
      })
    }
  }
  return { groupId, publishes: terminal, transitions }
}

/** 组任务真实接管占位后清除"结果待确认"标记(A06)。 */
function clearPendingConfirm(obj: CanvasObj): void {
  if (obj.pendingConfirm) {
    obj.pendingConfirm = false
  }
}

/**
 * 对账兜底:组占位绑定的任务已不在服务端任务列表里(终结任务只保留最近 200 个,
 * 列表查询也有 limit)→ 转错误卡,避免永远停在"生成中…"。仍在线/已在列表中的不动。
 */
export function sweepLostGroupSlots(
  doc: { objects: Record<string, CanvasObj> },
  serverJobs: JobLike[],
): { changedObjIds: string[] } {
  const known = new Set(serverJobs.map((j) => j.id))
  const changedObjIds: string[] = []
  for (const obj of Object.values(doc.objects)) {
    if (obj.kind !== 'placeholder' || !obj.groupId || !obj.gen?.jobId) {
      continue
    }
    if (known.has(obj.gen.jobId)) {
      continue
    }
    obj.kind = 'error'
    obj.errorDetail = '任务记录已过期,无法找回该方向的结果'
    delete obj.gen
    obj.height = Math.max(120, Math.min(obj.height, 160))
    changedObjIds.push(obj.id)
  }
  return { changedObjIds }
}

/**
 * 对账入口:把服务端任务列表按组应用(组任务整组语义,单任务走原 reconcile)。
 */
export function applyGroupJobs(
  doc: { objects: Record<string, CanvasObj> },
  serverJobs: JobLike[],
  resolveImage: ResolveImage,
): { changedObjIds: string[]; publishedGroups: string[] } {
  const changedObjIds: string[] = []
  const publishedGroups: string[] = []
  for (const [groupId, slots] of groupJobs(serverJobs)) {
    const result = groupTransitions(doc, slots, groupId)
    for (const { objId, transition } of result.transitions) {
      const obj = doc.objects[objId]
      if (!obj) {
        continue
      }
      // 服务端任务真实接管该槽位:待确认标记不再有意义(A06)
      clearPendingConfirm(obj)
      if (transition.kind === 'progress') {
        obj.gen = {
          ...(obj.gen || {}),
          jobId: obj.gen?.jobId || transition.jobId || '',
          progress: undefined,
          message: transition.message,
        }
      } else if (transition.kind === 'fail') {
        obj.kind = 'error'
        obj.errorDetail = transition.error
        delete obj.gen
        obj.height = Math.max(120, Math.min(obj.height, 160))
      } else if (transition.kind === 'complete') {
        const resolved = resolveImage(transition.image)
        if (resolved) {
          obj.kind = resolved.kind || 'image'
          obj.assetId = resolved.assetId || undefined
          obj.src = resolved.src
          obj.imageId = resolved.imageId || undefined
          obj.hasThumbs = resolved.hasThumbs !== false
          obj.ext = resolved.ext
          obj.width = resolved.width || obj.width
          obj.height = resolved.height || obj.height
          delete obj.gen
        } else {
          continue
        }
      }
      changedObjIds.push(objId)
    }
    if (result.publishes) {
      publishedGroups.push(groupId)
    }
  }
  return { changedObjIds, publishedGroups }
}
