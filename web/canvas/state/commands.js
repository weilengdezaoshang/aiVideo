// 文档命令(纯函数,可 node:test):DocStore 的全部变更都经由命令,
// 撤销 = 应用逆命令。SSE 驱动的变更(生成落位/进度)不走命令、不进 undo 栈(PRD §8.3)。
//
// 命令为纯数据对象,便于合并(coalesce)与序列化调试。

/** @typedef {{ x: number, y: number }} Point2D */

/**
 * @typedef {Object} CanvasObj 画布对象(PRD §7.1)
 * @property {string} id
 * @property {'image'|'video'|'placeholder'|'error'|'board'|'text'} kind
 * @property {number} x
 * @property {number} y
 * @property {number} width
 * @property {number} height
 * @property {string} [assetId]
 * @property {boolean} [hasThumbs] 资产是否带缩略图(无则只用原图)
 * @property {string} [ext] 资产原始扩展名
 * @property {string} [src] 直连图片 URL(stress/素材库拖入)
 * @property {string} [errorDetail] 错误卡原因文案
 * @property {string} [imageId] 历史记录 id(隐式反馈关联用)
 * @property {{fromId: string, params: object}} [lineage]
 * @property {{jobId: string, progress?: number, message?: string}} [gen]
 * @property {string} [groupId] 四方向组 id(PRD §5)
 * @property {number} [slot] 组内槽位 0..3(横排顺序固定)
 * @property {string} [directionTitle] 方向标题(槽位左上标签)
 * @property {string} [name] 画板名称
 * @property {string} [text] 文字内容
 * @property {number} [fontSize] 文字字号
 * @property {string} [fill] 文字颜色(空 = 跟随主题默认)
 * @property {{originalAssetId: string, originalExt: string, maskAssetId: string}} [cutout] 抠图谱系
 * @property {number} [durationSec] 视频时长(角标辨认用,PRD §8)
 */

/** 添加对象(含 order 追加);invert = 移除 @param {CanvasObj[]} objects */
export function cmdAddObjects(objects) {
  return { type: 'addObjects', objects }
}

/** 按 id 移除对象;invert = 加回(带原顺序位置) @param {{ object: CanvasObj, orderIndex: number }[]} entries */
export function cmdRemoveObjects(entries) {
  // entries: [{object, orderIndex}]
  return { type: 'removeObjects', entries }
}

/** 移动对象;invert = from/to 互换 @param {{ id: string, from: Point2D, to: Point2D }[]} moves */
export function cmdMoveObjects(moves) {
  // moves: [{id, from: {x,y}, to: {x,y}}]
  return { type: 'moveObjects', moves }
}

/**
 * 更新对象字段(浅 patch);invert = prevPatch
 * @param {string} id @param {Record<string, unknown>} patch @param {Record<string, unknown>} prevPatch
 */
export function cmdUpdateObject(id, patch, prevPatch) {
  return { type: 'updateObject', id, patch, prevPatch }
}

/**
 * 批量原子命令:顺序应用、一次撤销(PRD §6/A09:撤销派生结果时
 * 同时撤销其布局变更/顺延位移,不留半步)。invert = 逆序逐条取逆。
 * @param {any[]} commands
 */
export function cmdBatch(commands) {
  return { type: 'batch', commands }
}

/**
 * 就地应用命令到 doc(直接变更,DocStore 负责通知);未知命令抛错防御。
 * @param {{ objects: Record<string, CanvasObj>, order: string[] }} doc
 * @param {any} cmd
 */
export function applyCommand(doc, cmd) {
  switch (cmd.type) {
    case 'batch':
      for (const child of cmd.commands) {
        applyCommand(doc, child)
      }
      break
    case 'addObjects':
      for (const obj of cmd.objects) {
        // 浅克隆:命令载荷不与文档活对象共享引用,后续瞬态变更(SSE 落位)
        // 不会污染命令快照,undo/redo 才能恢复创建时的原貌
        doc.objects[obj.id] = { ...obj }
        if (!doc.order.includes(obj.id)) {
          doc.order.push(obj.id)
        }
      }
      break
    case 'removeObjects':
      for (const entry of cmd.entries) {
        delete doc.objects[entry.object.id]
        const idx = doc.order.indexOf(entry.object.id)
        if (idx >= 0) {
          doc.order.splice(idx, 1)
        }
      }
      break
    case 'moveObjects':
      for (const move of cmd.moves) {
        const obj = doc.objects[move.id]
        if (obj) {
          obj.x = move.to.x
          obj.y = move.to.y
        }
      }
      break
    case 'updateObject': {
      const obj = doc.objects[cmd.id]
      if (!obj) {
        throw new Error(`updateObject: 对象不存在 ${cmd.id}`)
      }
      Object.assign(obj, cmd.patch)
      break
    }
    default:
      throw new Error(`未知命令类型:${cmd.type}`)
  }
}

/** 逆命令(用于撤销);幂等要求命令生成时带上完整前后值。 @param {any} cmd @returns {any} */
export function invertCommand(cmd) {
  switch (cmd.type) {
    case 'addObjects':
      return cmdRemoveObjects(
        cmd.objects.map((/** @type {CanvasObj} */ obj) => ({ object: obj, orderIndex: -1 })),
      )
    case 'removeObjects':
      return cmdAddObjects(cmd.entries.map((/** @type {{ object: CanvasObj }} */ e) => e.object))
    case 'moveObjects':
      return cmdMoveObjects(
        cmd.moves.map((/** @type {{ id: string, from: Point2D, to: Point2D }} */ m) => ({
          id: m.id,
          from: m.to,
          to: m.from,
        })),
      )
    case 'updateObject':
      return cmdUpdateObject(cmd.id, cmd.prevPatch, cmd.patch)
    case 'batch':
      return cmdBatch([...cmd.commands].reverse().map((/** @type {any} */ child) => invertCommand(child)))
    default:
      throw new Error(`未知命令类型:${cmd.type}`)
  }
}

/** 两命令是否可合并为一步(连续拖动同一组对象) @param {any} prev @param {any} next */
export function canCoalesce(prev, next) {
  if (prev.type !== next.type) {
    return false
  }
  if (prev.type === 'moveObjects') {
    const prevIds = prev.moves
      .map((/** @type {{ id: string }} */ m) => m.id)
      .sort()
      .join(',')
    const nextIds = next.moves
      .map((/** @type {{ id: string }} */ m) => m.id)
      .sort()
      .join(',')
    return prevIds === nextIds && prevIds !== ''
  }
  return false
}

/** 将 next 合并进 prev(就地更新 prev 的终值) @param {any} prev @param {any} next */
export function coalesceInto(prev, next) {
  if (prev.type === 'moveObjects' && next.type === 'moveObjects') {
    const byId = new Map(
      next.moves.map((/** @type {{ id: string, to: Point2D }} */ m) => [m.id, m]),
    )
    for (const move of prev.moves) {
      const merged = byId.get(move.id)
      if (merged) {
        move.to = merged.to
      }
    }
  }
}

export {}
