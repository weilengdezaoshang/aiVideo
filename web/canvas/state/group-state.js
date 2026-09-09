// 四方向组状态(纯函数,可 node:test;PRD §5.2):
//   - 组 = 同一 groupId 的任务集合,slot 0..3 横排固定;
//   - 未全部终结 → 四格统一状态文案(准备中 / 生成中…),不逐张显露进度;
//   - 全部终结 → 一次性产出全部槽位的落位/失败 transition(整组发布),
//     已完成资产由服务端持久化,但前端共同呈现,不虚构 1/4 完成。 
// 占位对象 id = job.clientRef(与单图任务同约定);对象已被用户删除的槽位跳过,不复活。 

/**
 * @typedef JobLike
 * @property {string} id
 * @property {string} status
 * @property {number} [progress]
 * @property {string} [message]
 * @property {string} [error]
 * @property {any[]} [images]
 * @property {string} [groupId]
 * @property {number} [slot]
 * @property {string} [directionTitle]
 * @property {string} [clientRef]
 */

/** 按 groupId 分桶(保持 slot 排序)。 @param {JobLike[]} jobs */
export function groupJobs(jobs) {
  /** @type {Map<string, JobLike[]>} */
  const map = new Map()
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

/** @param {JobLike[]} slots @returns {boolean} 组内任务是否全部终结 */
export function isGroupTerminal(slots) {
  return slots.every((j) => j.status === 'completed' || j.status === 'failed')
}

/** 组的统一状态文案:全部排队 = 准备中;任一开始执行 = 生成中…(PRD §5.2)。 */
/** @param {JobLike[]} slots */
export function groupUnifiedMessage(slots) {
  if (slots.some((j) => j.status === 'running' || j.status === 'completed')) {
    return '生成中…'
  }
  return '准备中'
}

/** 组整体状态(对话卡展示)。 @param {JobLike[]} slots */
export function groupCardState(slots) {
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

/**
 * 计算一组任务当前应对画布占位应用的 transition 列表。 
 * 未终结 → 每个槽位一条统一 progress;全部终结 → 每槽位一条 complete/fail。 
 * complete 携带原始 image 记录,由调用方(applyGroupJobs)解析为资产字段。 
 * @param {{ objects: Record<string, any> }} doc
 * @param {JobLike[]} slots 同组任务(≥1)
 * @param {string} groupId
 * @returns {{ groupId: string, publishes: boolean, transitions: Array<{ objId: string, transition: any }> }}
 */
export function groupTransitions(doc, slots, groupId) {
  const terminal = isGroupTerminal(slots)
  const message = groupUnifiedMessage(slots)
  /** @type {Array<{ objId: string, transition: any }>} */
  const transitions = []
  for (const job of slots) {
    const objId = job.clientRef
    const obj = objId ? doc.objects[objId] : null
    if (!obj || obj.kind !== 'placeholder' || obj.groupId !== groupId) {
      continue // 槽位对象不存在或已被替换:跳过,不复活
    }
    if (!terminal) {
      transitions.push({
        objId: /** @type {string} */ (objId),
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
        objId: /** @type {string} */ (objId),
        transition: image
          ? { kind: 'complete', image }
          : { kind: 'fail', error: '任务完成但缺少产物' },
      })
    } else {
      transitions.push({
        objId: /** @type {string} */ (objId),
        transition: { kind: 'fail', error: job.error || '生成失败' },
      })
    }
  }
  return { groupId, publishes: terminal, transitions }
}

/**
 * 对账入口:把服务端任务列表按组应用(组任务整组语义,单任务走原 reconcile)。 
 * @param {{ objects: Record<string, any> }} doc
 * @param {JobLike[]} serverJobs
 * @param {(image: any) => any} resolveImage
 * @returns {{ changedObjIds: string[], publishedGroups: string[] }}
 */
export function applyGroupJobs(doc, serverJobs, resolveImage) {
  const changedObjIds = []
  const publishedGroups = []
  for (const [groupId, slots] of groupJobs(serverJobs)) {
    const result = groupTransitions(doc, slots, groupId)
    for (const { objId, transition } of result.transitions) {
      const obj = doc.objects[objId]
      if (!obj) {
        continue
      }
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
        const resolved = resolveImage?.(transition.image)
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
