// 对象层:把 DocStore 的对象渲染为 Konva 节点,承载选择/框选/拖动/删除交互。
// 选择集合是 TransientStore 语义(不进 undo、不入文档)。
// 性能(F1:500 对象 + 50 张 2048px 图 ≥55fps):
//   - 静态对象(image/video)整组 cache() 成位图,绘制退化为一次 drawImage;
//     相机缩放跨越 2× 阈值时限流重烘;
//   - 占位/错误卡数量少且内容常变,不缓存;
//   - 选中框画在独立覆盖层(overlayLayer),不污染缓存内容;
//   - 视口裁剪:远缩放全覆盖时靠位图缓存兜底,近景靠 visible 剔除。

import { cmdMoveObjects, cmdRemoveObjects } from '../state/commands.js'

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

/** @param {CanvasObj} obj @returns {boolean} 内容是否静态可缓存 */
const isCacheable = (obj) => obj.kind === 'image' || obj.kind === 'video'

/**
 * @param {{
 *   engine: any,
 *   getDoc: () => { objects: Record<string, CanvasObj>, order: string[] },
 *   applyCommand?: (cmd: any) => void,
 *   onSelectionChange?: (ids: string[]) => void,
 *   onCancelPlaceholder?: (objId: string) => void,
 *   onDeletePlaceholder?: (objId: string) => void,
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
      if (obj.kind === 'video') {
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
        text: '排队中',
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
    }
    wireGroupEvents(group)
    return group
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
              { label: '删除', run: options.onDeletePlaceholder },
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
    stroke: '#0066cc',
    strokeWidth: 1,
    dash: [4, 3],
    fill: 'rgba(0,102,204,0.06)',
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
    for (const id of selected) {
      const rect = selectionRects.get(id)
      const o = nodeById(id)
      if (rect && o) {
        rect.position({ x: o.x, y: o.y })
      }
    }
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
      if (!o || selectionRects.has(id)) {
        continue
      }
      const rect = new Konva.Rect({
        x: o.x,
        y: o.y,
        width: o.width,
        height: o.height,
        stroke: '#0066cc',
        strokeWidth: 2,
        strokeScaleEnabled: false,
        cornerRadius: 6,
        listening: false,
      })
      selectionRects.set(id, rect)
      overlayLayer.add(rect)
    }
    overlayLayer.batchDraw()
    onSelectionChange([...selected])
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
      // 占位 → 图片/错误卡等 kind 迁移需要重建组(spinner 等子节点无法复用)
      if (group && group.getAttr('builtKind') !== obj.kind) {
        group.destroy()
        groups.delete(id)
        cachedAtScale.delete(id)
        group = null
      }
      if (!group) {
        group = buildGroup(obj)
        group.setAttr('builtKind', obj.kind)
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
    applySelection()
    // 同步 draw + hit:Konva 10 的 hit 画布是惰性重绘(等真实指针事件),
    // 文档变更后必须显式刷新,否则命中检测漏掉新建/重建的对象
    layer.draw()
    layer.drawHit()
    overlayLayer.draw()
  }

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
      const text =
        obj.gen?.progress != null
          ? `${obj.gen.message || '生成中'} ${Math.round(obj.gen.progress * 100)}%`
          : '排队中'
      if (label && label.text() !== text) {
        label.text(text)
      }
    } else if (obj.kind === 'error') {
      const detail = group.getChildren((/** @type {any} */ n) => n.getClassName() === 'Text')[1]
      const text = obj.errorDetail || '未知原因'
      if (detail && detail.text() !== text) {
        detail.text(text)
      }
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
          options.onDeletePlaceholder?.(id)
        }
      }
      const removable = ids.filter(
        (id) => doc.objects[id] && doc.objects[id].kind !== 'placeholder',
      )
      if (removable.length > 0) {
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
