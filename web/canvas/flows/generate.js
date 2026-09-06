// 生成提交流程(T8 文生图 / T9 图生图入口):
// 提交 = 创建占位对象(用户命令,可撤销)+ POST /api/generate(clientRef=占位 id)+
// jobId 回填(瞬态,与命令合并不可拆——撤销提交即删除占位,PRD §8.3)。
// 画布提交强制 batchCount=1(多张 = 多任务,PRD §8.4-6)。

import { cmdAddObjects, cmdRemoveObjects } from '../state/commands.js'
import { findFreePosition } from '../state/placement.js'

const LOCAL_ID_PREFIX = 'obj-'
let localSeq = 0

/**
 * @param {{
 *   docStore: { doc: any, apply: (cmd: any, opts?: any) => void, mutateTransient: (fn: (doc: any) => unknown) => unknown },
 *   genParams: Record<string, unknown>,
 *   anchor?: { x: number, y: number },
 *   initImage?: { dataUrl: string },
 *   maskImage?: { dataUrl: string },
 *   lineage?: { fromId: string, params?: object },
 *   fetchImpl?: typeof fetch,
 * }} options
 * @returns {Promise<{ ok: boolean, objId?: string, jobId?: string, error?: string }>}
 */
export async function submitGeneration(options) {
  const {
    docStore,
    genParams,
    anchor = { x: 0, y: 0 },
    initImage,
    maskImage,
    lineage,
    fetchImpl = (...args) => fetch(...args),
  } = options

  const width = Number(genParams.width) || 512
  const height = Number(genParams.height) || 512
  const pos = findFreePosition(docStore.doc, anchor, { width, height })
  const objId = `${LOCAL_ID_PREFIX}${Date.now().toString(36)}-${++localSeq}`

  /** @type {import('../state/commands.js').CanvasObj} 占位对象;kind 需精确字面量类型 */
  const placeholder = {
    id: objId,
    kind: /** @type {'placeholder'} */ ('placeholder'),
    x: pos.x,
    y: pos.y,
    width,
    height,
    lineage: lineage && { params: {}, ...lineage },
  }
  docStore.apply(cmdAddObjects([placeholder]))

  try {
    const res = await fetchImpl('/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...genParams,
        batchCount: 1,
        clientRef: objId,
        initImage: initImage ? initImage.dataUrl : undefined,
        maskImage: maskImage ? maskImage.dataUrl : undefined,
      }),
    })
    const body = await res.json().catch(() => ({}))
    if (!res.ok) {
      // 提交失败:占位转为错误卡,参数保留供重试
      docStore.mutateTransient((doc) => {
        const obj = doc.objects[objId]
        if (obj) {
          obj.kind = 'error'
          obj.errorDetail = body.error || `提交失败 HTTP ${res.status}`
          delete obj.gen
        }
      })
      return { ok: false, objId, error: body.error || `HTTP ${res.status}` }
    }
    // jobId 回填(瞬态):SSE 可能已先到,handleJobEvent 靠 clientRef 兜底
    docStore.mutateTransient((doc) => {
      const obj = doc.objects[objId]
      // SSE 事件可能先于本响应到达并已写入 jobId/进度:只在首次回填
      if (obj && obj.kind === 'placeholder' && !obj.gen?.jobId) {
        obj.gen = { jobId: body.jobId, progress: 0, message: '排队中' }
      }
    })
    return { ok: true, objId, jobId: body.jobId }
  } catch (err) {
    docStore.mutateTransient((doc) => {
      const obj = doc.objects[objId]
      if (obj) {
        obj.kind = 'error'
        obj.errorDetail = (err instanceof Error ? err.message : String(err)) || '网络错误'
        delete obj.gen
      }
    })
    return { ok: false, objId, error: String(err) }
  }
}

/**
 * 取消任务并移除占位对象(悬浮「取消」/ Delete 删除占位时调用)。
 * 任务取消失败不阻塞本地移除(任务可能已结束)。
 * @param {{ docStore: any, objId: string, fetchImpl?: typeof fetch }} options
 */
export async function cancelPlaceholder(options) {
  const { docStore, objId, fetchImpl = (...args) => fetch(...args) } = options
  const obj = docStore.doc.objects[objId]
  const jobId = obj?.gen?.jobId
  if (jobId) {
    await fetchImpl(`/api/jobs/${jobId}`, { method: 'DELETE' }).catch(() => {})
  }
  if (docStore.doc.objects[objId]) {
    docStore.apply(
      cmdRemoveObjects([
        { object: docStore.doc.objects[objId], orderIndex: docStore.doc.order.indexOf(objId) },
      ]),
    )
  }
}

/**
 * 重试错误卡(线框 S7/S16):按谱系参数原样重发。
 * 错误卡 → 占位(瞬态,撤销栈不涉及:重试成功前画布上仍是"待处理"状态)。
 * 参考图不存于 lineage(体积),由调用方按 fromId 现取。
 * @param {{
 *   docStore: any,
 *   objId: string,
 *   resolveReference?: (fromId: string) => Promise<{ dataUrl: string } | null>,
 *   fetchImpl?: typeof fetch,
 * }} options
 */
export async function retryError(options) {
  const { docStore, objId, resolveReference, fetchImpl = (...args) => fetch(...args) } = options
  const errObj = docStore.doc.objects[objId]
  if (!errObj || errObj.kind !== 'error') {
    return { ok: false, error: '对象不存在或不可重试' }
  }
  const params = errObj.lineage?.params
  if (!params) {
    return { ok: false, error: '缺少谱系参数,无法重试' }
  }
  // 原位转回占位;结果落位仍走螺旋探测(锚点 = 原错误卡位置)
  const anchor = { x: errObj.x, y: errObj.y }
  const lineage = errObj.lineage
  docStore.mutateTransient((/** @type {any} */ doc) => {
    const obj = doc.objects[objId]
    if (obj) {
      obj.kind = 'placeholder'
      delete obj.errorDetail
      obj.gen = { jobId: '', progress: 0, message: '排队中' }
    }
  })
  let initImage
  if (lineage.fromId && resolveReference) {
    try {
      initImage = await resolveReference(lineage.fromId)
    } catch {
      initImage = null // 参考图不可得:退化为纯文生图重试
    }
  }
  const result = await submitGeneration._into(docStore, {
    existingId: objId,
    genParams: params,
    anchor,
    initImage: initImage || undefined,
    lineage,
    fetchImpl,
  })
  return result
}

/**
 * submitGeneration 的内部变体:复用已有对象 id(重试场景)。
 * @param {any} docStore
 * @param {{ existingId: string, genParams: Record<string, unknown>, anchor: { x: number, y: number },
 *           initImage?: { dataUrl: string }, lineage?: { fromId: string, params?: object },
 *           fetchImpl: typeof fetch }} spec
 */
submitGeneration._into = async function (docStore, spec) {
  const { existingId, genParams, initImage, fetchImpl } = spec
  try {
    const res = await fetchImpl('/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...genParams,
        batchCount: 1,
        clientRef: existingId,
        initImage: initImage ? initImage.dataUrl : undefined,
      }),
    })
    const body = await res.json().catch(() => ({}))
    if (!res.ok) {
      docStore.mutateTransient((/** @type {any} */ doc) => {
        const obj = doc.objects[existingId]
        if (obj) {
          obj.kind = 'error'
          obj.errorDetail = body.error || `提交失败 HTTP ${res.status}`
          delete obj.gen
        }
      })
      return { ok: false, objId: existingId, error: body.error || `HTTP ${res.status}` }
    }
    docStore.mutateTransient((/** @type {any} */ doc) => {
      const obj = doc.objects[existingId]
      if (obj && obj.kind === 'placeholder') {
        obj.gen = { jobId: body.jobId, progress: 0, message: '排队中' }
      }
    })
    return { ok: true, objId: existingId, jobId: body.jobId }
  } catch (err) {
    docStore.mutateTransient((/** @type {any} */ doc) => {
      const obj = doc.objects[existingId]
      if (obj) {
        obj.kind = 'error'
        obj.errorDetail = (err instanceof Error ? err.message : String(err)) || '网络错误'
        delete obj.gen
      }
    })
    return { ok: false, objId: existingId, error: String(err) }
  }
}
