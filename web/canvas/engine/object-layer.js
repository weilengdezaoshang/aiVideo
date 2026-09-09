// 对象层:把 DocStore 的对象渲染为 Konva 节点,承载选择/框选/拖动/删除交互。 
// 选择集合是 TransientStore 语义(不进 undo、不入文档)。 
// 性能(F1:500 对象 + 50 张 2048px 图 ≥55fps):
//   - 静态对象(image/video)整组 cache() 成位图,绘制退化为一次 drawImage;
//     相机缩放跨越 2× 阈值时限流重烘;
//   - 占位/错误卡数量少且内容常变,不缓存;
//   - 选中框画在独立覆盖层(overlayLayer),不污染缓存内容;
//   - 视口裁剪:远缩放全覆盖时靠位图缓存兜底,近景靠 visible 剔除。 

import {
  cmdBatch,
  cmdMoveObjects,
  cmdRemoveObjects,
  cmdUpdateObject,
} from '../state/commands.js'

/**
 * @typedef {import('../state/commands.js').CanvasObj} CanvasObj
 * @typedef {import('../state/doc-store.js').DocStoreEvent} DocStoreEvent
 */

const MARGIN = 80 // 视口裁剪外扩(px),避免边缘闪烁
/** 缩略图三档:显示宽(屏幕px)≤384 用 256 图,≤1536 用 1024 图,更大用原图 */
const THUMB256_MAX_PX = 384
const THUMB1024_MAX_PX = 1536
const RECACHE_BUDGET_PER_FRAME = 6 // 每帧最多重烘数量,防止缩放风暴
const DPR = typeof devicePixelRatio === 'number' ? devicePixelRatio : 1
/** 组标题与内容/派生标签的视觉常量(PRD §4.2/§6) */
const GROUP_TITLE_OFFSET = 26

/** @param {CanvasObj} obj @returns {boolean} 内容是否静态可缓存 */
const isCacheable = (obj) => obj.kind === 'image' || obj.kind === 'video'

/** 主题感知的画板底色(随 data-theme 变化,内容由文档持有)。  */
function boardFill() {
  return document.documentElement.dataset.theme === 'dark' ? '#25262c' : '#ffffff'
}

/** 主题感知的画布文字默认色;已保存的文字对象颜色不随主题改变。  */
function textInk() {
  return document.documentElement.dataset.theme === 'dark' ? '#f0f1f4' : '#202124'
}

/**
 * 使用 Konva 节点的实时坐标同步选中框。 拖动结束前文档坐标仍是旧值，不能作为位置来源。 
 * @param {Set<string>} selected
 * @param {Map<string, any>} selectionRects
 * @param {Map<string, any>} groups
 * @param {{ batchDraw: () => void }} overlayLayer
 */
export function syncSelectionRectPositions(selected, selectionRects, groups, overlayLayer) {
  for (const id of selected) {
    const rect = selectionRects.get(id)
    const group = groups.get(id)
    if (rect && group) {
      rect.position({ x: group.x(), y: group.y() })
    }
  }
  overlayLayer.batchDraw()
}

/**
 * @param {{
 *   engine: any,
 *   getDoc: () => { objects: Record<string, CanvasObj>, order: string[], groups?: Record<string, any> },
 *   applyCommand?: (cmd: any) => void,
 *   onSelectionChange?: (ids: string[]) => void,
 *   onCancelPlaceholder?: (objId: string) => void,
 *   onCancelTransient?: (objId: string) => void, // 占位=级联取消;错误卡=移除
 *   onObjectsDeleted?: (objs: CanvasObj[]) => void, // 生成结果被删除(隐式质量反馈)
 *   onRetryError?: (objId: string) => void,
 *   imageUrlsFor?: (obj: CanvasObj) => { thumb256?: string | null, thumb1024?: string | null, original: string },
 * }} options
 */
export function createObjectLayer(options) {
  const { engine, getDoc, onSelectionChange = () => {}, imageUrlsFor } = options
  const layer = engine.contentLayer
  /** 选中框/框选矩形画在这层,始终在内容之上 */
  const overlayLayer = new Konva.Layer({ listening: false })
  engine.stage.add(overlayLayer)
  engine.registerCamLayer(overlayLayer)

  /** @type {Map<string, any>} */
  const groups = new Map()
  /** @type {Map<string, any>} id → 选中框矩形 */
  const selectionRects = new Map()
  /** @type {Map<string, HTMLImageElement>} 按 URL 缓存解码位图 */
  const imageCache = new Map()
  /** @type {Map<string, number>} id → 烘焙时的相机 scale */
  const cachedAtScale = new Map()
  /** @type {Set<string>} 当前选中 */
  const selected = new Set()
  /** @type {string | null} 正在拖动的主对象 */
  let draggingId = null
  /** @type {{ id: string, from: { x: number, y: number }, node: any }[] | null} */
  let dragStarts = null

  const nodeById = (/** @type {string} */ id) => getDoc().objects[id]

  /* ---------- 图片加载(缩略图优先) ---------- */

  /** @param {CanvasObj} obj @returns {string | null} */
  function pickUrl(obj) {
    if (imageUrlsFor) {
      const urls = imageUrlsFor(obj)
      const displayW = obj.width * engine.getCam().scale
      if (displayW > THUMB1024_MAX_PX && urls.original) {
        return urls.original
      }
      if (displayW > THUMB256_MAX_PX && urls.thumb1024) {
        return urls.thumb1024
      }
      if (urls.thumb256) {
        return urls.thumb256
      }
      return urls.original
    }
    return obj.assetId ? `/assets/${obj.assetId}/original.png` : null
  }

  /** @param {string} url @returns {HTMLImageElement} */
  function loadImage(url) {
    const cached = imageCache.get(url)
    if (cached) {
      return cached
    }
    const img = new Image()
    img.decoding = 'async'
    img.src = url
    imageCache.set(url, img)
    return img
  }

  /* ---------- 节点构建 ---------- */

  /** @param {CanvasObj} obj */
  function buildGroup(obj) {
    const group = new Konva.Group({ id: obj.id, x: obj.x, y: obj.y, draggable: true })
    const bg = new Konva.Rect({
      width: obj.width,
      height: obj.height,
      fill: '#fff',
      stroke: 'rgba(0,0,0,0.08)',
      strokeWidth: 1,
      cornerRadius: 6,
      // 绘制热路径全面降耗:不做完美绘制/命中描边/描边阴影
      perfectDrawEnabled: false,
      hitStrokeEnabled: false,
      shadowForStrokeEnabled: false,
    })
    group.add(bg)

    if (obj.kind === 'image' || obj.kind === 'video') {
      const imageNode = new Konva.Image({
        width: obj.width,
        height: obj.height,
        cornerRadius: 6,
        perfectDrawEnabled: false,
      })
      group.add(imageNode)
      // 组内方向标签在发布后保留(图片标签在左上,PRD §4.2)
      if (obj.directionTitle) {
        group.add(directionChip(obj.directionTitle, obj.width))
      }
      if (obj.kind === 'video') {
        // 视频辨认(PRD §8/A15):▶ + 时长与文件格式角标
        const duration = Number(
          obj.durationSec ?? (/** @type {any} */ (obj.lineage?.params))?.durationSec ?? 0,
        )
        const badge = new Konva.Label({
          x: 8,
          y: obj.height - 26,
          opacity: 0.88,
          listening: false,
          perfectDrawEnabled: false,
        })
        badge.add(new Konva.Tag({ fill: 'rgba(0,0,0,0.62)', cornerRadius: 8 }))
        badge.add(
          new Konva.Text({
            text: `${duration > 0 ? `${duration}s · ` : ''}${String(obj.ext || 'mp4').toUpperCase()}`,
            padding: 3,
            fontSize: 11,
            fill: '#fff',
          }),
        )
        group.add(badge)
        const playIcon = new Konva.Text({
          text: '▶',
          fontSize: Math.min(28, obj.height / 4),
          fill: '#fff',
          listening: false,
        })
        playIcon.offsetX(playIcon.width() / 2)
        playIcon.offsetY(playIcon.height() / 2)
        playIcon.position({ x: obj.width / 2, y: obj.height / 2 })
        group.add(playIcon)
      }
    } else if (obj.kind === 'board') {
      // 画板 = 背景容器:不裁剪、不自动收养重叠对象(PRD §4.2)
      bg.fill(boardFill())
      bg.stroke('rgba(53,109,243,0.35)')
      bg.cornerRadius(12)
      const name = new Konva.Text({
        text: obj.name || '画板',
        x: 12,
        y: 10,
        fontSize: 13,
        fontStyle: '600',
        fill: textInk(),
        listening: false,
        opacity: 0.72,
      })
      group.add(name)
      group.setAttr('boardName', name)
      group.setAttr('bgRect', bg)
    } else if (obj.kind === 'text') {
      bg.fill('rgba(0,0,0,0)')
      bg.stroke('rgba(0,0,0,0)')
      const textNode = new Konva.Text({
        text: obj.text || '',
        fontSize: obj.fontSize || 32,
        fontFamily: 'inherit',
        lineHeight: 1.3,
        fill: obj.fill || textInk(),
        align: 'left',
        listening: true,
        perfectDrawEnabled: false,
      })
      group.add(textNode)
      group.setAttr('textNode', textNode)
    } else if (obj.kind === 'placeholder') {
      const spinner = new Konva.Arc({
        x: obj.width / 2,
        y: obj.height / 2 - 10,
        innerRadius: 10,
        outerRadius: 14,
        angle: 260,
        fill: '#1d1d1f',
        rotation: 0,
        listening: false,
      })
      const label = new Konva.Text({
        text: obj.gen?.message || '准备中',
        width: obj.width,
        align: 'center',
        y: obj.height / 2 + 12,
        fontSize: 13,
        fill: '#7a7a7a',
        listening: false,
      })
      group.add(spinner, label)
      group.setAttr('spinner', spinner)
      group.setAttr('label', label)
      // 方向标签(四方向槽位左上,PRD §5.1)
      if (obj.directionTitle) {
        group.add(directionChip(obj.directionTitle, obj.width))
      }
      if (obj.groupId) {
        group.add(slotChip((obj.slot ?? 0) + 1, obj.width))
      }
    } else if (obj.kind === 'error') {
      bg.dash([6, 4])
      bg.stroke('#d63031')
      const title = new Konva.Text({
        text: '生成失败',
        width: obj.width,
        align: 'center',
        y: obj.height / 2 - 22,
        fontSize: 14,
        fontStyle: 'bold',
        fill: '#d63031',
        listening: false,
      })
      const detail = new Konva.Text({
        text: obj.errorDetail || '未知原因',
        width: obj.width,
        align: 'center',
        y: obj.height / 2 - 2,
        fontSize: 12,
        fill: '#7a7a7a',
        listening: false,
      })
      group.add(title, detail)
      if (obj.directionTitle) {
        group.add(directionChip(obj.directionTitle, obj.width))
      }
    }
    wireGroupEvents(group)
    return group
  }

  /** 方向标签(左上角小胶囊)。 @param {string} title @param {number} width */
  function directionChip(title, width) {
    const text = String(title).slice(0, 8)
    const chip = new Konva.Label({
      x: 8,
      y: 8,
      opacity: 0.9,
      listening: false,
      perfectDrawEnabled: false,
    })
    chip.add(new Konva.Tag({ fill: '#1d1d1f', cornerRadius: 8 }))
    chip.add(new Konva.Text({ text, padding: 3, fontSize: 11, fill: '#fff' }))
    // 超宽图上标签贴左上即可,不需要特殊处理宽度
    void width
    return chip
  }

  /** 槽位序号标签。 @param {number} slot @param {number} width */
  function slotChip(slot, width) {
    const chip = new Konva.Label({
      x: Math.max(8, width - 30),
      y: 8,
      opacity: 0.85,
      listening: false,
      perfectDrawEnabled: false,
    })
    chip.add(new Konva.Tag({ fill: 'rgba(53,109,243,0.9)', cornerRadius: 8 }))
    chip.add(new Konva.Text({ text: String(slot), padding: 3, fontSize: 11, fill: '#fff' }))
    return chip
  }

  /* ---------- 位图缓存管理 ---------- */

  /** 烘焙像素比:随当前缩放,放大不超采样 */
  function cachePixelRatio() {
    return Math.min(2, Math.max(0.25, engine.getCam().scale * DPR))
  }

  /** @param {CanvasObj} obj @param {any} group */
  function cacheGroup(obj, group) {
    if (!isCacheable(obj) || group.getAttr('caching')) {
      return
    }
    group.setAttr('caching', true)
    try {
      // drawBorder 是调试边框(红色),必须关掉
      group.cache({ pixelRatio: cachePixelRatio(), offset: 4 })
      // Konva 10 的 hit 画布是懒创建:必须显式 drawHit 才会物化,否则命中检测漏掉整个对象
      layer.drawHit()
      cachedAtScale.set(obj.id, engine.getCam().scale)
    } finally {
      group.setAttr('caching', false)
    }
  }

  /** 缩放跨 2× 后的重烘(每帧限流) */
  function refreshStaleCaches() {
    const cam = engine.getCam()
    let budget = RECACHE_BUDGET_PER_FRAME
    for (const [id, atScale] of cachedAtScale) {
      if (budget <= 0) {
        break
      }
      const group = groups.get(id)
      const obj = nodeById(id)
      if (!group || !obj) {
        cachedAtScale.delete(id)
        continue
      }
      const ratio = cam.scale / atScale
      if (ratio > 2 || ratio < 0.5) {
        group.clearCache()
        cachedAtScale.delete(id)
        cacheGroup(obj, group)
        budget--
      }
    }
  }

  /* ---------- 对象事件:选择 / 拖动 ---------- */

  /** @param {any} group */
  function wireGroupEvents(group) {
    group.on('mousedown touchstart', (/** @type {{ evt: any }} */ e) => {
      const id = group.id()
      if (e.evt.button === 2) {
        return // 右键交给 contextmenu
      }
      if (!selected.has(id)) {
        if (e.evt.shiftKey) {
          selected.add(id)
        } else {
          selected.clear()
          selected.add(id)
        }
        applySelection()
      }
      group.moveToTop()
    })

    group.on('dragstart', () => {
      draggingId = group.id()
      dragStarts = [...selected]
        .filter((id) => getDoc().objects[id])
        .map((id) => {
          const o = getDoc().objects[id]
          return { id, from: { x: o.x, y: o.y }, node: groups.get(id) }
        })
    })

    group.on('dragmove', () => {
      if (!draggingId || !dragStarts) {
        return
      }
      const main = dragStarts.find((/** @type {{ id: string }} */ d) => d.id === draggingId)
      if (!main || !main.node) {
        return
      }
      const dx = main.node.x() - main.from.x
      const dy = main.node.y() - main.from.y
      for (const item of dragStarts) {
        if (item.id === draggingId || !item.node) {
          continue
        }
        item.node.position({ x: item.from.x + dx, y: item.from.y + dy })
      }
      syncSelectionRects()
    })

    // 占位悬浮「取消」/ 错误卡悬浮「重试·删除」(PRD §5.4 / 线框 S7)
    group.on('mouseenter', () => {
      const o = nodeById(group.id())
      if (group.getAttr('cancelBtn') || (o?.kind !== 'placeholder' && o?.kind !== 'error')) {
        return
      }
      const actions =
        o.kind === 'placeholder'
          ? [{ label: '取消', run: options.onCancelPlaceholder }]
          : [
              { label: '重试', run: options.onRetryError },
              { label: '删除', run: options.onCancelTransient },
            ]
      const btn = new Konva.Label({ x: o.width, y: 0, opacity: 0.92, listening: true })
      btn.add(
        new Konva.Tag({
          fill: '#1d1d1f',
          cornerRadius: 4,
          pointerDirection: 'left',
          pointerWidth: 4,
          pointerHeight: 6,
        }),
      )
      btn.add(
        new Konva.Text({
          text: actions.map((a) => a.label).join(' · '),
          padding: 4,
          fontSize: 12,
          fill: '#fff',
        }),
      )
      btn.on('click tap', (/** @type {any} */ ev) => {
        ev.cancelBubble = true
        const pointer = group.getStage().getPointerPosition()
        const rel = btn.getAbsoluteTransform().copy().invert().point(pointer)
        const per = btn.width() / actions.length
        const idx = Math.min(actions.length - 1, Math.max(0, Math.floor(rel.x / per)))
        actions[idx].run?.(group.id())
      })
      group.add(btn)
      group.setAttr('cancelBtn', btn)
      layer.batchDraw()
    })
    group.on('mouseleave', () => {
      const btn = group.getAttr('cancelBtn')
      if (btn) {
        btn.destroy()
        group.setAttr('cancelBtn', null)
        layer.batchDraw()
      }
    })

    group.on('dragend', () => {
      if (!dragStarts) {
        return
      }
      const moves = dragStarts
        .filter((/** @type {{ node: any }} */ d) => d.node)
        .map((d) => ({
          id: d.id,
          from: d.from,
          to: { x: d.node.x(), y: d.node.y() },
        }))
        .filter((m) => m.from.x !== m.to.x || m.from.y !== m.to.y)
      draggingId = null
      dragStarts = null
      if (moves.length > 0) {
        options.applyCommand?.(cmdMoveObjects(moves))
      }
    })
  }

  /* ---------- 框选(空白处按下拖出) ---------- */

  const marquee = new Konva.Rect({
    stroke: '#356df3',
    strokeWidth: 1,
    dash: [4, 3],
    fill: 'rgba(53,109,243,0.06)',
    visible: false,
    listening: false,
  })
  overlayLayer.add(marquee)

  // 空白处按下(target = Stage,不经过 Layer 冒泡)开始框选。 
  // 移动/抬起监听 window:指针移出画布甚至窗口后松开也能正常收尾;
  // 会话序号保证残留的旧监听自废弃;平移手势(空格/中键)期间不启动。 
  let marqueeSeq = 0
  engine.stage.on('mousedown touchstart', (/** @type {{ target: any, evt: any }} */ e) => {
    const button = e.evt.button
    if (
      e.target !== engine.stage ||
      (button !== undefined && button !== 0) ||
      engine.isPanActive()
    ) {
      return
    }
    const seq = ++marqueeSeq
    const startWorld = engine.screenToWorld(engine.stage.getPointerPosition())
    marquee.visible(true)
    marquee.setAttrs({ x: startWorld.x, y: startWorld.y, width: 0, height: 0 })
    overlayLayer.batchDraw()

    const move = () => {
      if (seq !== marqueeSeq) {
        return cleanup()
      }
      const now = engine.screenToWorld(engine.stage.getPointerPosition())
      marquee.setAttrs({
        x: Math.min(startWorld.x, now.x),
        y: Math.min(startWorld.y, now.y),
        width: Math.abs(now.x - startWorld.x),
        height: Math.abs(now.y - startWorld.y),
      })
      overlayLayer.batchDraw()
    }
    const cleanup = () => {
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', up)
      window.removeEventListener('touchmove', move)
      window.removeEventListener('touchend', up)
    }
    const up = () => {
      if (seq !== marqueeSeq) {
        return cleanup()
      }
      cleanup()
      marquee.visible(false)
      const box = marquee.getAttrs()
      if (box.width > 6 || box.height > 6) {
        selected.clear()
        for (const o of Object.values(getDoc().objects)) {
          if (
            o.x < box.x + box.width &&
            o.x + o.width > box.x &&
            o.y < box.y + box.height &&
            o.y + o.height > box.y
          ) {
            selected.add(o.id)
          }
        }
      } else {
        selected.clear()
      }
      applySelection()
      overlayLayer.batchDraw()
    }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
    window.addEventListener('touchmove', move, { passive: true })
    window.addEventListener('touchend', up)
  })

  /* ---------- 选择呈现(覆盖层矩形,不碰缓存) ---------- */

  function syncSelectionRects() {
    syncSelectionRectPositions(selected, selectionRects, groups, overlayLayer)
  }

  function applySelection() {
    for (const [id, rect] of selectionRects) {
      if (!selected.has(id) || !nodeById(id)) {
        rect.destroy()
        selectionRects.delete(id)
      }
    }
    for (const id of selected) {
      const o = nodeById(id)
      if (!o) {
        continue
      }
      const rect = selectionRects.get(id)
      if (rect) {
        // 已存在的选框同步最新几何(缩放/改尺寸后不残留旧框)
        rect.setAttrs({ x: o.x, y: o.y, width: o.width, height: o.height })
        continue
      }
      const created = new Konva.Rect({
        x: o.x,
        y: o.y,
        width: o.width,
        height: o.height,
        stroke: '#356df3',
        strokeWidth: 2,
        strokeScaleEnabled: false,
        cornerRadius: 6,
        listening: false,
      })
      selectionRects.set(id, created)
      overlayLayer.add(created)
    }
    syncResizeHandles()
    overlayLayer.batchDraw()
    onSelectionChange([...selected])
  }

  /* ---------- 对象把手:画板改尺寸 / 图片等比缩放(PRD §4.2) ---------- */

  /** @type {Map<string, any>} 画板右下角尺寸把手 */
  const resizeHandles = new Map()
  /** @type {Map<string, any>} 图片/视频等比缩放把手(仅单选) */
  const scaleHandles = new Map()

  /** 控制点屏幕尺寸固定:世界尺寸随相机缩放反算(PRD §4.2)。 */
  function handleSize() {
    return 12 / Math.max(0.05, engine.getCam().scale)
  }

  function syncResizeHandles() {
    const size = handleSize()
    for (const [id, handle] of resizeHandles) {
      const o = nodeById(id)
      if (!selected.has(id) || !o || o.kind !== 'board') {
        handle.destroy()
        resizeHandles.delete(id)
        continue
      }
      handle.setAttrs({ x: o.x + o.width, y: o.y + o.height, width: size, height: size })
    }
    for (const [id, handle] of scaleHandles) {
      const o = nodeById(id)
      const stillValid =
        selected.has(id) && o && (o.kind === 'image' || o.kind === 'video') && selected.size === 1
      if (!stillValid) {
        handle.destroy()
        scaleHandles.delete(id)
        continue
      }
      handle.setAttrs({ x: o.x + o.width, y: o.y + o.height, width: size, height: size })
    }
    for (const id of selected) {
      const o = nodeById(id)
      if (!o || resizeHandles.has(id)) {
        continue
      }
      if (o.kind === 'board') {
        const handle = createHandle(id, o, 'board')
        resizeHandles.set(id, handle)
        overlayLayer.add(handle)
      } else if ((o.kind === 'image' || o.kind === 'video') && selected.size === 1) {
        const handle = createHandle(id, o, 'scale')
        scaleHandles.set(id, handle)
        overlayLayer.add(handle)
      }
    }
  }

  /**
   * @param {string} id
   * @param {CanvasObj} o
   * @param {'board' | 'scale'} kind
   */
  function createHandle(id, o, kind) {
    const size = handleSize()
    const handle = new Konva.Rect({
      x: o.x + o.width,
      y: o.y + o.height,
      width: size,
      height: size,
      fill: kind === 'scale' ? '#356df3' : '#ffffff',
      stroke: '#356df3',
      strokeWidth: 2,
      cornerRadius: 3,
      draggable: true,
    })
    if (kind === 'board') {
      wireBoardResize(handle, id)
    } else {
      wireUniformScale(handle, id)
    }
    return handle
  }

  /** @param {any} handle @param {string} id */
  function wireBoardResize(handle, id) {
    handle.on('dragmove', () => {
      const obj = nodeById(id)
      const bg = groups.get(id)?.getAttr('bgRect')
      if (!obj || !bg) {
        return
      }
      const min = 64 / Math.max(0.05, engine.getCam().scale)
      const w = Math.max(min, handle.x() - obj.x)
      const h = Math.max(min, handle.y() - obj.y)
      bg.width(w)
      bg.height(h)
      overlayLayer.batchDraw()
    })
    handle.on('dragend', () => {
      const obj = nodeById(id)
      const bg = groups.get(id)?.getAttr('bgRect')
      if (!obj || !bg) {
        return
      }
      const min = 64 / Math.max(0.05, engine.getCam().scale)
      const w = Math.max(min, Math.round(handle.x() - obj.x))
      const h = Math.max(min, Math.round(handle.y() - obj.y))
      options.applyCommand?.(
        cmdUpdateObject(id, { width: w, height: h }, { width: obj.width, height: obj.height }),
      )
    })
  }

  /**
   * 图片/视频等比缩放:拖角预览(组节点缩放),松手提交尺寸并让同组右侧成员顺延;
   * 只改显示尺寸,不改资产分辨率(PRD §4.2)。
   * @param {any} handle @param {string} id
   */
  function wireUniformScale(handle, id) {
    let ratio = 1
    handle.on('dragmove', () => {
      const obj = nodeById(id)
      const g = groups.get(id)
      if (!obj || !g) {
        return
      }
      const dx = (handle.x() - obj.x) / obj.width
      const dy = (handle.y() - obj.y) / obj.height
      ratio = Math.min(8, Math.max(0.1, Math.max(dx, dy)))
      g.scale({ x: ratio, y: ratio })
      overlayLayer.batchDraw()
    })
    handle.on('dragend', () => {
      const obj = nodeById(id)
      const g = groups.get(id)
      if (!obj || !g) {
        return
      }
      g.scale({ x: 1, y: 1 })
      const w = Math.max(48, Math.round(obj.width * ratio))
      const h = Math.max(48, Math.round(obj.height * ratio))
      const delta = w - obj.width
      /** @type {any[]} */
      const commands = []
      if (obj.groupId) {
        const moves = Object.values(getDoc().objects)
          .filter(
            (/** @type {any} */ m) =>
              m.id !== id && m.groupId === obj.groupId && m.x >= obj.x + obj.width,
          )
          .map((/** @type {any} */ m) => ({
            id: m.id,
            from: { x: m.x, y: m.y },
            to: { x: m.x + delta, y: m.y },
          }))
        if (moves.length > 0) {
          commands.push(cmdMoveObjects(moves))
        }
      }
      commands.push(
        cmdUpdateObject(id, { width: w, height: h }, { width: obj.width, height: obj.height }),
      )
      // 顺延位移与尺寸变更一次撤销(PRD §6/A09)
      options.applyCommand?.(commands.length > 1 ? cmdBatch(commands) : commands[0])
      ratio = 1
    })
  }

  /* ---------- 视口裁剪与占位转圈动画 ---------- */

  let spinnerAnimRunning = false
  // Konva.Animation 不消费回调返回值,停止完全由 recomputeVisibility 控制
  const spinnerAnim = new Konva.Animation(() => {
    for (const group of groups.values()) {
      const spinner = group.getAttr('spinner')
      if (spinner && group.visible()) {
        spinner.rotation((spinner.rotation() + 4) % 360)
      }
    }
  }, layer)

  function recomputeVisibility() {
    const cam = engine.getCam()
    const pad = MARGIN / cam.scale
    const minX = cam.x - pad
    const minY = cam.y - pad
    const maxX = cam.x + engine.stage.width() / cam.scale + pad
    const maxY = cam.y + engine.stage.height() / cam.scale + pad
    // 相机缩放后把手尺寸(屏幕恒定)需要重算
    if (resizeHandles.size > 0 || scaleHandles.size > 0) {
      syncResizeHandles()
    }
    let spinnerVisible = false
    for (const [id, group] of groups) {
      const o = nodeById(id)
      if (!o) {
        continue
      }
      const visible = o.x + o.width > minX && o.x < maxX && o.y + o.height > minY && o.y < maxY
      group.visible(visible)
      if (visible && group.getAttr('spinner')) {
        spinnerVisible = true
      }
    }
    if (spinnerVisible && !spinnerAnimRunning) {
      spinnerAnim.start()
      spinnerAnimRunning = true
    } else if (!spinnerVisible && spinnerAnimRunning) {
      spinnerAnim.stop()
      spinnerAnimRunning = false
    }
  }

  /* ---------- 组标题(PRD §4.2:点组标题选组,拖组标题移动整组) ---------- */

  /** @type {Map<string, any>} groupId → 标题 Konva.Label */
  const titleNodes = new Map()

  /** 每次同步重建组标题(组数量少,重建成本可忽略;成员边界实时计算)。  */
  function syncGroupTitles() {
    const doc = getDoc()
    const memberBounds = new Map()
    for (const obj of Object.values(doc.objects)) {
      if (!obj.groupId) {
        continue
      }
      const b = memberBounds.get(obj.groupId) ?? {
        minX: Infinity,
        minY: Infinity,
        maxX: -Infinity,
      }
      b.minX = Math.min(b.minX, obj.x)
      b.minY = Math.min(b.minY, obj.y)
      b.maxX = Math.max(b.maxX, obj.x + obj.width)
      memberBounds.set(obj.groupId, b)
    }
    const seen = new Set()
    for (const [groupId, bounds] of memberBounds) {
      seen.add(groupId)
      const title = doc.groups?.[groupId]?.title || '四方向生成'
      let node = titleNodes.get(groupId)
      const wanted = `${title}`
      if (node && node.getAttr('title') !== wanted) {
        node.destroy()
        node = null
      }
      if (!node) {
        node = new Konva.Label({
          x: bounds.minX,
          y: bounds.minY - GROUP_TITLE_OFFSET,
          draggable: true,
        })
        node.add(
          new Konva.Tag({
            fill: 'rgba(32,33,36,0.66)',
            cornerRadius: 9,
          }),
        )
        node.add(
          new Konva.Text({
            text: wanted,
            padding: 4,
            fontSize: 12,
            fill: '#fff',
          }),
        )
        node.setAttr('title', wanted)
        node.setAttr('groupId', groupId)
        wireTitleEvents(node, groupId)
        titleNodes.set(groupId, node)
        layer.add(node)
      }
      node.position({ x: bounds.minX, y: bounds.minY - GROUP_TITLE_OFFSET })
      node.moveToTop()
    }
    for (const [groupId, node] of titleNodes) {
      if (!seen.has(groupId)) {
        node.destroy()
        titleNodes.delete(groupId)
        selected.delete(`title:${groupId}`)
      }
    }
  }

  /**
   * 标题节点交互:点 = 选整组;拖 = 移动整组(一个命令,可撤销)。 
   * @param {any} node @param {string} groupId
   */
  function wireTitleEvents(node, groupId) {
    const memberIds = () =>
      Object.values(getDoc().objects)
        .filter((o) => o.groupId === groupId)
        .map((o) => o.id)

    node.on('mousedown touchstart', (/** @type {any} */ e) => {
      if (e.evt.button === 2) {
        return
      }
      e.cancelBubble = true
      selected.clear()
      for (const id of memberIds()) {
        selected.add(id)
      }
      applySelection()
    })

    /** @type {{ from: Map<string, {x:number,y:number}>, origin: {x:number,y:number} } | null} */
    let titleDrag = null
    node.on('dragstart', () => {
      const from = new Map()
      for (const id of memberIds()) {
        const o = getDoc().objects[id]
        const g = groups.get(id)
        from.set(id, { x: o.x, y: o.y })
        // 视觉先行:命令在 dragend 提交
        if (g) {
          g.setAttr('titleDragPeer', true)
        }
      }
      titleDrag = { from, origin: { x: node.x(), y: node.y() } }
    })
    node.on('dragmove', () => {
      if (!titleDrag) {
        return
      }
      const dx = node.x() - titleDrag.origin.x
      const dy = node.y() - titleDrag.origin.y
      for (const [id, pos] of titleDrag.from) {
        groups.get(id)?.position({ x: pos.x + dx, y: pos.y + dy })
      }
      syncSelectionRects()
    })
    node.on('dragend', () => {
      if (!titleDrag) {
        return
      }
      const dx = node.x() - titleDrag.origin.x
      const dy = node.y() - titleDrag.origin.y
      const moves = [...titleDrag.from.entries()]
        .map(([id, pos]) => ({ id, from: pos, to: { x: pos.x + dx, y: pos.y + dy } }))
        .filter((m) => m.from.x !== m.to.x || m.from.y !== m.to.y)
      titleDrag = null
      if (moves.length > 0) {
        options.applyCommand?.(cmdMoveObjects(moves))
      } else {
        sync()
      }
    })
  }

  /* ---------- 全量同步(doc → 节点) ---------- */

  function sync() {
    const doc = getDoc()
    const seen = new Set()
    for (const id of doc.order) {
      const obj = doc.objects[id]
      if (!obj) {
        continue
      }
      seen.add(id)
      let group = groups.get(id)
      // kind 迁移(占位→图片)或尺寸变化(等比缩放/画板改尺寸)需要重建组:
      // 子节点(背景矩形/图片/角标)的尺寸在 build 时固定,无法就地复用
      if (
        group &&
        (group.getAttr('builtKind') !== obj.kind ||
          group.getAttr('builtW') !== obj.width ||
          group.getAttr('builtH') !== obj.height)
      ) {
        group.destroy()
        groups.delete(id)
        cachedAtScale.delete(id)
        group = null
      }
      if (!group) {
        group = buildGroup(obj)
        group.setAttr('builtKind', obj.kind)
        group.setAttr('builtW', obj.width)
        group.setAttr('builtH', obj.height)
        groups.set(id, group)
        layer.add(group)
      }
      if (id !== draggingId) {
        group.position({ x: obj.x, y: obj.y })
      }
      if (obj.kind !== 'placeholder') {
        const staleBtn = group.getAttr('cancelBtn')
        if (staleBtn) {
          staleBtn.destroy()
          group.setAttr('cancelBtn', null)
        }
      }
      refreshContent(obj, group)
    }
    for (const [id, group] of groups) {
      if (!seen.has(id)) {
        group.destroy()
        groups.delete(id)
        cachedAtScale.delete(id)
        selected.delete(id)
        selectionRects.get(id)?.destroy()
        selectionRects.delete(id)
      }
    }
    recomputeVisibility()
    refreshStaleCaches()
    syncGroupTitles()
    applySelection()
    // 同步 draw + hit:Konva 10 的 hit 画布是惰性重绘(等真实指针事件),
    // 文档变更后必须显式刷新,否则命中检测漏掉新建/重建的对象
    layer.draw()
    layer.drawHit()
    overlayLayer.draw()
  }

  // 主题切换:重建画板底色 / 未定制文字颜色(视觉内容像素不随主题变化,PRD §4.1.1)
  document.addEventListener('gencanvas:themechange', () => {
    for (const [id, group] of groups) {
      group.destroy()
      groups.delete(id)
      cachedAtScale.delete(id)
    }
    sync()
  })

  /**
   * 内容级刷新:图片 URL 按显示尺寸升级、占位进度文案、缓存触发。 
   * @param {CanvasObj} obj @param {any} group
   */
  function refreshContent(obj, group) {
    if (obj.kind === 'image' || obj.kind === 'video') {
      const imageNode = group.getChildren((/** @type {any} */ n) => n.getClassName() === 'Image')[0]
      if (!imageNode) {
        return
      }
      const url = pickUrl(obj)
      const current = imageNode.image()
      if (url && (!current || current.src !== url)) {
        const img = loadImage(url)
        const apply = () => {
          imageNode.image(img)
          cacheGroup(obj, group)
          layer.draw()
        }
        if (img.complete && img.naturalWidth > 0) {
          apply()
        } else {
          img.addEventListener('load', apply, { once: true })
        }
      } else if (current && !group.isCached()) {
        cacheGroup(obj, group)
      }
    } else if (obj.kind === 'placeholder') {
      const label = group.getAttr('label')
      // 组槽位只显示统一状态文案(不虚构百分比);单图任务保留进度百分比
      const text =
        obj.gen?.progress != null && !obj.groupId
          ? `${obj.gen.message || '生成中'} ${Math.round(obj.gen.progress * 100)}%`
          : obj.gen?.message || '排队中'
      if (label && label.text() !== text) {
        label.text(text)
      }
    } else if (obj.kind === 'error') {
      const detail = group.getChildren((/** @type {any} */ n) => n.getClassName() === 'Text')[1]
      const text = obj.errorDetail || '未知原因'
      if (detail && detail.text() !== text) {
        detail.text(text)
      }
    } else if (obj.kind === 'text') {
      const textNode = group.getAttr('textNode')
      if (textNode) {
        if (textNode.text() !== (obj.text || '')) {
          textNode.text(obj.text || '')
        }
        if (!obj.fill && textNode.fill() !== textInk()) {
          textNode.fill(textInk())
        }
        group.width?.(Math.max(obj.width, textNode.width() || 0))
      }
      // 文字对象没有位图背景:命中交给 textNode
    } else if (obj.kind === 'board') {
      const name = group.getAttr('boardName')
      const bgRect = group.getAttr('bgRect')
      if (name) {
        if (name.text() !== (obj.name || '画板')) {
          name.text(obj.name || '画板')
        }
        if (name.fill() !== textInk()) {
          name.fill(textInk())
        }
      }
      bgRect?.fill(boardFill())
    }
  }

  return {
    layer,
    overlayLayer,
    sync,
    getSelected: () => [...selected],
    recomputeVisibility,
    /** @param {string[]} ids */
    setSelected(ids) {
      selected.clear()
      for (const id of ids) {
        selected.add(id)
      }
      applySelection()
    },
    /** 删除当前选中(Delete 键/菜单);占位对象级联取消任务(PRD §5.4) */
    deleteSelected() {
      const ids = [...selected]
      if (ids.length === 0) {
        return
      }
      const doc = getDoc()
      // 占位对象级联取消;其余对象走命令删除
      for (const id of ids) {
        if (doc.objects[id]?.kind === 'placeholder') {
          options.onCancelTransient?.(id)
        }
      }
      const removable = ids.filter(
        (id) => doc.objects[id] && doc.objects[id].kind !== 'placeholder',
      )
      if (removable.length > 0) {
        // 生成结果的删除是隐式质量信号(保留率),上报给追踪层
        options.onObjectsDeleted?.(removable.map((id) => doc.objects[id]))
        options.applyCommand?.(
          cmdRemoveObjects(
            removable.map((id) => ({
              object: doc.objects[id],
              orderIndex: doc.order.indexOf(id),
            })),
          ),
        )
      }
    },
    /** 视野框住对象(生成落位后跟进) @param {string} id */
    revealObject(id) {
      const o = nodeById(id)
      if (!o) {
        return
      }
      const cam = engine.getCam()
      const margin = 80
      const needScale = Math.min(
        1,
        Math.min(
          (engine.stage.width() - margin * 2) / o.width,
          (engine.stage.height() - margin * 2) / o.height,
        ),
      )
      if (needScale < cam.scale * 0.9 || needScale > cam.scale * 1.2) {
        engine.centerOn({ x: o.x + o.width / 2, y: o.y + o.height / 2 }, Math.max(0.05, needScale))
      }
    },
  }
}
