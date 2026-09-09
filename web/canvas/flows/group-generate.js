// 四方向组生成提交流程(PRD §5):
//   提交 = 原位同时创建 4 个占位(同一 groupId,横排成组)+ POST /api/generate/group;
//   服务端规划四条方向,每方向一个任务;四占位状态由 SSE 组语义驱动
//   (group-state.js:统一"准备中/生成中…",全部终结才整组发布)。
//   组元数据(标题/需求/方向)随文档 groups 字段持久化,刷新后可恢复。

import { cmdAddObjects, cmdRemoveObjects } from '../state/commands.js'
import { findFreePosition } from '../state/placement.js'

const LOCAL_ID_PREFIX = 'obj-'
let localSeq = 0

/** 组内槽位横排间距(世界单位;派生插入间距 24,组行用 32 视觉更清晰) */
const GROUP_GAP = 32

/**
 * @param {{
 *   docStore: any,
 *   engine?: any,
 *   genParams: Record<string, unknown>,
 *   anchor?: { x: number, y: number },
 *   chat?: any,
 *   onPlaceholderCreated?: (slotIds: string[]) => void,
 *   fetchImpl?: typeof fetch,
 * }} options
 * @returns {Promise<{ ok: boolean, groupId?: string, slotIds?: string[], error?: string }>}
 */
export async function submitGroupGeneration(options) {
  const {
    docStore,
    genParams,
    anchor,
    chat,
    onPlaceholderCreated = () => {},
    fetchImpl = (...args) => fetch(...args),
  } = options

  const width = Number(genParams.width) || 512
  const height = Number(genParams.height) || 512
  const engine = options.engine
  let base = anchor
  if (!base && engine) {
    const cam = engine.getCam()
    base = {
      x: cam.x + engine.stage.width() / cam.scale / 2,
      y: cam.y + engine.stage.height() / cam.scale / 2,
    }
  }
  base = base ?? { x: 0, y: 0 }

  // 整行占位先整体找空位(4 宽 + 3 间距),再切成四个锚点 → 横排成组不压其他对象
  const rowSize = { width: width * 4 + GROUP_GAP * 3, height }
  const rowPos = findFreePosition(docStore.doc, base, rowSize, { gap: GROUP_GAP })
  const groupId = `grp-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
  const slotIds = Array.from(
    { length: 4 },
    () => `${LOCAL_ID_PREFIX}${Date.now().toString(36)}-${++localSeq}-${Math.random().toString(36).slice(2, 6)}`,
  )

  /** @type {any[]} */
  const placeholders = slotIds.map((id, slot) => ({
    id,
    kind: /** @type {'placeholder'} */ ('placeholder'),
    x: rowPos.x + slot * (width + GROUP_GAP),
    y: rowPos.y,
    width,
    height,
    groupId,
    slot,
    gen: { jobId: '', message: '准备中' },
  }))
  docStore.apply(cmdAddObjects(placeholders))
  // 组元数据随文档持久化(标题 = 需求截断;方向待服务端规划回填)
  docStore.mutateTransient((/** @type {any} */ doc) => {
    doc.groups = doc.groups || {}
    doc.groups[groupId] = {
      title: String(genParams.prompt || '').slice(0, 40) || '四方向生成',
      prompt: genParams.prompt || '',
      createdAt: new Date().toISOString(),
      slots: slotIds,
    }
  })
  chat?.addGroupCard(groupId, slotIds, String(genParams.prompt || ''))
  onPlaceholderCreated(slotIds)

  /** 提交失败(明确 4xx/5xx,服务端已拒绝):撤下四个占位;保留组元数据可重试 */
  /** @param {string} message */
  function abort(message) {
    docStore.apply(
      cmdRemoveObjects(
        slotIds
          .filter((id) => docStore.doc.objects[id])
          .map((id) => ({
            object: docStore.doc.objects[id],
            orderIndex: docStore.doc.order.indexOf(id),
          })),
      ),
    )
    chat?.addError(message)
    return { ok: false, error: message }
  }

  /**
   * 提交未收到确认(网络错误/超时,请求可能已到达服务端,PRD §5.2 A06):
   * 占位保留为"结果待确认",由组卡「查询结果」显式核对,不自动重提、不重复提交。
   */
  /** @param {string} detail */
  function markPendingConfirm(detail) {
    docStore.mutateTransient((/** @type {any} */ doc) => {
      for (const id of slotIds) {
        const o = doc.objects[id]
        if (o && o.kind === 'placeholder') {
          o.gen = { jobId: '', message: '结果待确认' }
          o.pendingConfirm = true
        }
      }
    })
    chat?.addSystem(
      `提交未收到确认(${detail})。占位已保留,请用组卡「查询结果」核对;确认前请勿重复提交。`,
    )
    return { ok: false, pendingConfirm: true, groupId, slotIds }
  }

  try {
    const res = await fetchImpl('/api/generate/group', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...genParams, batchCount: 1, seed: -1, clientRefs: slotIds }),
    })
    const body = await res.json().catch(() => ({}))
    if (!res.ok) {
      return abort(body.error || `提交失败 HTTP ${res.status}`)
    }
    // 回填方向元数据(标题/维度),占位标签换成方向名
    docStore.mutateTransient((/** @type {any} */ doc) => {
      const meta = doc.groups?.[groupId]
      if (meta) {
        meta.subject = body.subject
        meta.constraints = body.constraints
        meta.directions = body.slots
      }
      for (const slot of body.slots || []) {
        const obj = doc.objects[slotIds[slot.slot]]
        if (obj) {
          obj.directionTitle = slot.title
          if (obj.gen && !obj.gen.message?.includes('生成中')) {
            obj.gen.message = '准备中'
          }
        }
      }
    })
    return { ok: true, groupId, slotIds }
  } catch (err) {
    // fetch 抛错 = 网络层失败,请求可能已到达服务端:进入"结果待确认"(A06)
    return markPendingConfirm((err instanceof Error ? err.message : String(err)) || '网络错误')
  }
}

/**
 * 「查询结果」(A06:未知请求只查询,不自动重发):
 * 按槽位 clientRef 在服务端任务列表中核对本次提交。
 *   - 找到任务 → 清除待确认标记并交还给对账流程(整组语义接管);
 *   - 确认未入队 → 安全清理占位(保留组元数据,可"另生成一组")。
 * @param {{
 *   docStore: any,
 *   groupId: string,
 *   chat?: any,
 *   fetchImpl?: typeof fetch,
 * }} options
 * @returns {Promise<{ ok: boolean, found?: boolean }>}
 */
export async function confirmPendingGroup(options) {
  const { docStore, groupId, chat, fetchImpl = (...args) => fetch(...args) } = options
  const meta = docStore.doc.groups?.[groupId]
  const slotIds = /** @type {string[]} */ (meta?.slots) || []
  if (!Array.isArray(slotIds) || slotIds.length === 0) {
    return { ok: false }
  }
  /** @type {any[]} */
  let jobs = []
  try {
    const res = await fetchImpl('/api/jobs?all=1&limit=100')
    const body = await res.json()
    jobs = body.jobs || []
  } catch (err) {
    chat?.addError(
      `查询失败:${err instanceof Error ? err.message : String(err)},请稍后再次查询`,
    )
    return { ok: false }
  }
  const mine = jobs.filter((/** @type {any} */ j) => slotIds.includes(j.clientRef))
  if (mine.length === 0) {
    docStore.apply(
      cmdRemoveObjects(
        slotIds
          .filter((/** @type {string} */ id) => docStore.doc.objects[id])
          .map((/** @type {string} */ id) => ({
            object: docStore.doc.objects[id],
            orderIndex: docStore.doc.order.indexOf(id),
          })),
      ),
    )
    chat?.addSystem('服务端没有该次请求的记录,已清理占位;可从组卡「另生成一组」重新生成。')
    return { ok: true, found: false }
  }
  docStore.mutateTransient((/** @type {any} */ doc) => {
    for (const id of slotIds) {
      const o = doc.objects[id]
      if (o) {
        o.pendingConfirm = false
        if (o.gen) {
          o.gen.message = '准备中'
        }
      }
    }
  })
  return { ok: true, found: true }
}

/**
 * 重生成一组(PRD §5.2 部分失败建议路径):用原组的需求新建一组,
 * 旧组与成功资产保留;明确告知将产生新的生成调用(费用由所选服务决定)。
 * @param {{ docStore: any, groupId: string, chat?: any, engine?: any, fetchImpl?: typeof fetch }} options
 */
export async function regenerateGroup(options) {
  const { docStore, groupId, chat, engine, fetchImpl } = options
  const meta = docStore.doc.groups?.[groupId]
  const prompt = meta?.prompt
  if (!prompt) {
    chat?.addError('找不到原组的需求,无法重生成')
    return { ok: false }
  }
  const first = (meta.slots || [])
    .map((/** @type {string} */ id) => docStore.doc.objects[id])
    .find(Boolean)
  const genParams = {
    ...(first?.lineage?.params || {}),
    prompt,
    width: first?.width || 512,
    height: first?.height || 512,
    kind: 'image',
  }
  return submitGroupGeneration({ docStore, genParams, chat, engine, fetchImpl })
}
