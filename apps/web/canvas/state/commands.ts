import type { Storyboard } from './storyboard.js'
import type { Timeline } from './timeline.js'
// 文档命令(纯函数,可 node:test):DocStore 的全部变更都经由命令,
// 撤销 = 应用逆命令。SSE 驱动的变更(生成落位/进度)不走命令、不进 undo 栈(PRD §8.3)。
//
// 命令为纯数据对象,便于合并(coalesce)与序列化调试。

import type { NodeDraft, NodeRun } from './node-model.js'

/** @see {CanvasObj} 二维坐标 */
export type Point2D = { x: number; y: number }

/** 画布对象(PRD §7.1) */
export type CanvasObj = {
  id: string
  kind: 'image' | 'video' | 'placeholder' | 'error' | 'board' | 'text' | 'draft'
  /** 未生成节点的目标类型 */
  mediaType?: 'image' | 'video'
  x: number
  y: number
  width: number
  height: number
  /** 顺时针旋转角度,默认 0 */
  rotation?: number
  nodeDraft?: NodeDraft
  nodeRun?: NodeRun
  textCard?: boolean
  backgroundColor?: string
  assetId?: string
  /** 资产是否带缩略图(无则只用原图) */
  hasThumbs?: boolean
  /** 资产原始扩展名 */
  ext?: string
  /** 直连图片 URL(stress/素材库拖入) */
  src?: string
  /** 错误卡原因文案 */
  errorDetail?: string
  /** 历史记录 id(隐式反馈关联用) */
  imageId?: string
  lineage?: { fromId: string; params: object }
  /** 生成中占位的任务关联;jobId 在 POST 响应前可为空(事件回填) */
  gen?: { jobId?: string; progress?: number; message?: string }
  /** 生成任务的稳定关联；进度与文案存放在 GenerationRuntimeStore。 */
  generationRef?: { operationId: string; jobId?: string }
  /** 可持久化的生成输入，用于显式重试与谱系检查。 */
  generationSpec?: {
    mode: 'text-to-image' | 'image-to-image'
    params: Record<string, unknown>
    sourceObjectId?: string
  }
  /** 生成实体引用；placement 的布局独立于任务与产物状态。 */
  generationId?: string
  /** 四方向组 id(PRD §5) */
  groupId?: string
  /** 组内槽位 0..3(横排顺序固定) */
  slot?: number
  /** 方向标题(槽位左上标签) */
  directionTitle?: string
  /** 画板名称 */
  name?: string
  /** 文字内容 */
  text?: string
  /** 文字字号 */
  fontSize?: number
  /** 文字颜色(空 = 跟随主题默认) */
  fill?: string
  /** 抠图谱系 */
  cutout?: { originalAssetId: string; originalExt: string; maskAssetId: string }
  /** 视频时长(角标辨认用,PRD §8) */
  durationSec?: number
  /** 组任务接管前的"结果待确认"标记(A06) */
  pendingConfirm?: boolean
}

/** 添加对象(含 order 追加);invert = 移除 */
export function cmdAddObjects(objects: CanvasObj[]): AddObjectsCommand {
  return { type: 'addObjects', objects }
}

/** 按 id 移除对象;invert = 加回(带原顺序位置) */
export function cmdRemoveObjects(
  entries: { object: CanvasObj; orderIndex: number }[],
): RemoveObjectsCommand {
  return { type: 'removeObjects', entries }
}

/** 移动对象;invert = from/to 互换 */
export function cmdMoveObjects(
  moves: { id: string; from: Point2D; to: Point2D }[],
): MoveObjectsCommand {
  return { type: 'moveObjects', moves }
}

/** 更新对象字段(浅 patch);invert = prevPatch */
export function cmdUpdateObject(
  id: string,
  patch: Record<string, unknown>,
  prevPatch: Record<string, unknown>,
): UpdateObjectCommand {
  return { type: 'updateObject', id, patch, prevPatch }
}

/**
 * 批量原子命令:顺序应用、一次撤销(PRD §6/A09:撤销派生结果时
 * 同时撤销其布局变更/顺延位移,不留半步)。invert = 逆序逐条取逆。
 */
export function cmdBatch(commands: CanvasCommand[]): BatchCommand {
  return { type: 'batch', commands }
}

export function cmdGenerateNode(spec: {
  transactionId: string
  objectId: string
  generationId: string
  initialPlacement: CanvasObj
}): GenerateNodeCommand {
  return { type: 'generateNode', ...spec, initialPlacement: structuredClone(spec.initialPlacement) }
}

export type AddObjectsCommand = { type: 'addObjects'; objects: CanvasObj[] }
export type RemoveObjectsCommand = {
  type: 'removeObjects'
  entries: { object: CanvasObj; orderIndex: number }[]
}
export type MoveObjectsCommand = {
  type: 'moveObjects'
  moves: { id: string; from: Point2D; to: Point2D }[]
}
export type UpdateObjectCommand = {
  type: 'updateObject'
  id: string
  patch: Record<string, unknown>
  prevPatch: Record<string, unknown>
}
export type BatchCommand = { type: 'batch'; commands: CanvasCommand[] }
export type GenerateNodeCommand = {
  type: 'generateNode'
  transactionId: string
  objectId: string
  generationId: string
  initialPlacement: CanvasObj
}
export type SetTimelineCommand = { type: 'setTimeline'; before?: Timeline; after?: Timeline }
export type SetStoryboardCommand = {
  type: 'setStoryboard'
  before?: Storyboard
  after?: Storyboard
}
export type CanvasCommand =
  | SetStoryboardCommand
  | SetTimelineCommand
  | AddObjectsCommand
  | RemoveObjectsCommand
  | MoveObjectsCommand
  | UpdateObjectCommand
  | GenerateNodeCommand
  | BatchCommand

/** 可就地应用命令的最小文档形状 */
export type CommandDoc = {
  objects: Record<string, CanvasObj>
  order: string[]
  storyboard?: Storyboard
  timeline?: Timeline
}

/** 就地应用命令到 doc(直接变更,DocStore 负责通知);未知命令抛错防御。 */
export function applyCommand(doc: CommandDoc, cmd: CanvasCommand): void {
  switch (cmd.type) {
    case 'setStoryboard':
      if (cmd.after) {
        doc.storyboard = structuredClone(cmd.after)
      } else {
        delete doc.storyboard
      }
      break
    case 'setTimeline':
      if (cmd.after) {
        doc.timeline = structuredClone(cmd.after)
      } else {
        delete doc.timeline
      }
      break
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
    case 'generateNode': {
      doc.objects[cmd.objectId] = structuredClone(cmd.initialPlacement)
      if (!doc.order.includes(cmd.objectId)) {
        doc.order.push(cmd.objectId)
      }
      break
    }
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
  }
}

/** 逆命令(用于撤销);幂等要求命令生成时带上完整前后值。 */
export function invertCommand(cmd: CanvasCommand): CanvasCommand {
  switch (cmd.type) {
    case 'setStoryboard':
      return { type: 'setStoryboard', before: cmd.after, after: cmd.before }
    case 'setTimeline':
      return { type: 'setTimeline', before: cmd.after, after: cmd.before }
    case 'addObjects':
      return cmdRemoveObjects(cmd.objects.map((obj) => ({ object: obj, orderIndex: -1 })))
    case 'removeObjects':
      return cmdAddObjects(cmd.entries.map((e) => e.object))
    case 'moveObjects':
      return cmdMoveObjects(cmd.moves.map((m) => ({ id: m.id, from: m.to, to: m.from })))
    case 'updateObject':
      return cmdUpdateObject(cmd.id, cmd.prevPatch, cmd.patch)
    case 'generateNode': {
      return cmdRemoveObjects([{ object: structuredClone(cmd.initialPlacement), orderIndex: -1 }])
    }
    case 'batch':
      return cmdBatch([...cmd.commands].reverse().map((child) => invertCommand(child)))
  }
}

/** 两命令是否可合并为一步(连续拖动同一组对象) */
export function canCoalesce(prev: CanvasCommand, next: CanvasCommand): boolean {
  if (prev.type !== next.type) {
    return false
  }
  if (prev.type === 'moveObjects' && next.type === 'moveObjects') {
    const prevIds = prev.moves
      .map((m) => m.id)
      .sort()
      .join(',')
    const nextIds = next.moves
      .map((m) => m.id)
      .sort()
      .join(',')
    return prevIds === nextIds && prevIds !== ''
  }
  return false
}

/** 将 next 合并进 prev(就地更新 prev 的终值) */
export function coalesceInto(prev: CanvasCommand, next: CanvasCommand): void {
  if (prev.type === 'moveObjects' && next.type === 'moveObjects') {
    const byId = new Map(next.moves.map((m) => [m.id, m]))
    for (const move of prev.moves) {
      const merged = byId.get(move.id)
      if (merged) {
        move.to = merged.to
      }
    }
  }
}
