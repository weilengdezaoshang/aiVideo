import {
  isEmptyMedia,
  nodeAppearanceKey,
  nodeProgressLabel,
  placeholderSurface,
} from './node-appearance.js'
import {
  createVideoHoverPreview,
  hoverFinePointer,
  schedulePosterRecache,
  shouldStartVideoHover,
  shouldStopHoverForSourceChange,
} from './video-hover.js'
// 对象层:把 DocStore 的对象渲染为 Konva 节点,承载选择/框选/拖动/删除交互。
// 选择集合是 TransientStore 语义(不进 undo、不入文档)。
// 性能(F1:500 对象 + 50 张 2048px 图 ≥55fps):
//   - 静态对象(image/video)整组 cache() 成位图,绘制退化为一次 drawImage;
//     相机缩放跨越 2× 阈值时限流重烘;
//   - 占位/错误卡数量少且内容常变,不缓存;
//   - 选中框画在独立覆盖层(overlayLayer),不污染缓存内容;
//   - 视口裁剪:远缩放全覆盖时靠位图缓存兜底,近景靠 visible 剔除。

import {
  transformPoint,
  transformBounds,
  resizeFromCorner,
  rotateAroundCenter,
  resizeCursor,
  type Geometry,
} from './transform-geometry.js'
import type { CanvasEngine, Point } from './stage.js'

import {
  cmdBatch,
  cmdMoveObjects,
  cmdRemoveObjects,
  cmdUpdateObject,
  type CanvasCommand,
  type CanvasObj,
} from '../state/commands.js'

const MARGIN = 80 // 视口裁剪外扩(px),避免边缘闪烁
/** 缩略图三档:显示宽(屏幕px)≤384 用 256 图,≤1536 用 1024 图,更大用原图 */
const THUMB256_MAX_PX = 384
const THUMB1024_MAX_PX = 1536
const RECACHE_BUDGET_PER_FRAME = 6 // 每帧最多重烘数量,防止缩放风暴
const DPR = typeof devicePixelRatio === 'number' ? devicePixelRatio : 1
/** 组标题与内容/派生标签的视觉常量(PRD §4.2/§6) */
const GROUP_TITLE_OFFSET = 26

// Konva 以 UMD 全局引入且无类型来源:节点统一走宽松接口(见 globals.d.ts)。
/* eslint-disable @typescript-eslint/no-explicit-any -- Konva 节点边界 */

/** 内容是否静态可缓存 */
const isCacheable = (obj: CanvasObj) => obj.kind === 'image' || obj.kind === 'video'

/** 主题感知的画板底色(随 data-theme 变化,内容由文档持有)。 */
function boardFill() {
  return document.documentElement.dataset.theme === 'dark' ? '#25262c' : '#ffffff'
}

/** 主题感知的画布文字默认色;已保存的文字对象颜色不随主题改变。 */
function textInk() {
  return document.documentElement.dataset.theme === 'dark' ? '#f0f1f4' : '#202124'
}

export type ObjectLayerDoc = {
  objects: Record<string, CanvasObj>
  order: string[]
  groups?: Record<string, { title?: string }>
}

export type ObjectLayerOptions = {
  engine: CanvasEngine
  getDoc: () => ObjectLayerDoc
  applyCommand?: (cmd: CanvasCommand) => void
  onSelectionChange?: (ids: string[]) => void
  onCancelPlaceholder?: (objId: string) => void
  /** 占位=级联取消;错误卡=移除 */
  onCancelTransient?: (objId: string) => void
  /** 生成结果被删除(隐式质量反馈) */
  onObjectsDeleted?: (objs: CanvasObj[]) => void
  onRetryError?: (objId: string) => void
  imageUrlsFor?: (obj: CanvasObj) => {
    thumb256?: string | null
    thumb1024?: string | null
    original: string
  }
}

export type ObjectLayer = {
  layer: any
  overlayLayer: any
  sync(): void
  getSelected(): string[]
  recomputeVisibility(): void
  setSelected(ids: string[]): void
  /** 删除当前选中(Delete 键/菜单);占位对象级联取消任务(PRD §5.4) */
  deleteSelected(): void
  /** 视野框住对象(生成落位后跟进) */
  revealObject(id: string): void
  /** 蒙版模式内容层不接收事件,按视觉层级做对象几何命中,不改变 listening。 */
  pickObjectAt(world: Point): string | null
}

/**
 * 使用 Konva 节点的实时坐标同步选中框。 拖动结束前文档坐标仍是旧值，不能作为位置来源。
 */
export function syncSelectionRectPositions(
  selected: Set<string>,
  selectionRects: Map<string, any>,
  groups: Map<string, any>,
  overlayLayer: { batchDraw: () => void },
) {
  for (const id of selected) {
    const rect = selectionRects.get(id)
    const group = groups.get(id)
    if (rect && group) {
      rect.position({ x: group.x(), y: group.y() })
    }
  }
  overlayLayer.batchDraw()
}

export function createObjectLayer(options: ObjectLayerOptions): ObjectLayer {
  const { engine, getDoc, onSelectionChange = () => {}, imageUrlsFor } = options
  const layer = engine.contentLayer
  /** 选中框/框选矩形画在这层,始终在内容之上 */
  const overlayLayer = new Konva.Layer()
  engine.stage.add(overlayLayer)
  engine.registerCamLayer(overlayLayer)

  const groups = new Map<string, any>()
  /** id → 选中框矩形 */
  const selectionRects = new Map<string, any>()
  /** 按 URL 缓存解码位图 */
  const imageCache = new Map<string, HTMLImageElement>()
  const videoCache = new Map<string, HTMLVideoElement>()
  /** id → 烘焙时的相机 scale */
  const cachedAtScale = new Map<string, number>()
  /** 当前选中 */
  const selected = new Set<string>()
  /** 正在拖动的主对象 */
  let draggingId: string | null = null
  let dragStarts: { id: string; from: { x: number; y: number }; node: any }[] | null = null

  const nodeById = (id: string) => getDoc().objects[id]
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)')
  let loadingTime = 0
  const progressAnimation = new Konva.Animation((frame: { timeDiff: number } | undefined) => {
    const dt = Math.min(frame?.timeDiff || 0, 64)
    loadingTime += dt
    for (const group of groups.values()) {
      if (!group.visible() || !group.getAttr('loadingEffect')?.visible()) {
        continue
      }
      const trail = group.getAttr('progressTrail')
      const perimeter = group.getAttr('progressPerimeter') as number
      trail.dashOffset(trail.dashOffset() - (dt * perimeter) / 4200)
      for (const star of group.getAttr('loadingStars')) {
        const phase = star.getAttr('wavePhase') as number
        star.opacity(0.2 + 0.65 * ((1 + Math.sin(loadingTime / 750 - phase)) / 2))
      }
    }
  }, layer)
  const hoverAnimation = new Konva.Animation(() => {
    /* 逐帧重绘,让 Konva.Image 显示当前视频帧;顺带同步悬停播放的进度条. */
    updateHoverSeekBar()
  }, layer)
  const posterSeekCancels = new Map<string, () => void>()
  let activeHoverUrl: string | null = null
  const cancelPosterSeek = (id: string) => {
    posterSeekCancels.get(id)?.()
    posterSeekCancels.delete(id)
  }
  const videoHover = createVideoHoverPreview({
    onPlaying(id) {
      cancelPosterSeek(id)
      const group = groups.get(id)
      if (group) {
        group.clearCache()
        cachedAtScale.delete(id)
        group.getAttr('playIcon')?.visible(false)
        group.getAttr('pauseIcon')?.visible(false)
        group.getAttr('seekBar')?.visible(true)
        // 进度条可见前先按当前缩放排定尺寸,不依赖动画帧
        layoutSeekBar(group)
      }
      hoverAnimation.start()
      layer.batchDraw()
    },
    onPaused(id) {
      const group = groups.get(id)
      group?.getAttr('pauseIcon')?.visible(true)
      layer.batchDraw()
    },
    onStopped(id, media, meta) {
      hoverAnimation.stop()
      const playedUrl = activeHoverUrl
      activeHoverUrl = null
      const group = groups.get(id)
      const obj = nodeById(id)
      group?.getAttr('playIcon')?.visible(true)
      group?.getAttr('pauseIcon')?.visible(false)
      // 进度条必须在重烘焙位图前隐藏,否则会被烘进封面
      group?.getAttr('seekBar')?.visible(false)
      cancelPosterSeek(id)
      if (!group || !obj || !isCacheable(obj) || isEmptyMedia(obj)) {
        layer.batchDraw()
        return
      }
      group.clearCache()
      cachedAtScale.delete(id)
      const captured = group
      posterSeekCancels.set(
        id,
        schedulePosterRecache(
          media,
          () => {
            const current = nodeById(id)
            if (!current || groups.get(id) !== captured) {
              return
            }
            cacheGroup(current, captured)
            layer.batchDraw()
          },
          () =>
            videoHover.activeId() === id ||
            groups.get(id) !== captured ||
            pickUrl(nodeById(id)) !== playedUrl,
          meta.seekRequested,
        ),
      )
      layer.batchDraw()
    },
  })
  const startHoverPreview = (id: string, url: string, video: HTMLVideoElement) => {
    videoHover.start(id, video)
    if (videoHover.activeId() === id) {
      activeHoverUrl = url
    }
  }

  /** 悬停播放时同步底部进度条的填充宽度与滑块位置. */
  function updateHoverSeekBar() {
    const id = videoHover.activeId()
    if (!id) {
      return
    }
    const group = groups.get(id)
    const fill = group?.getAttr('seekFill')
    const thumb = group?.getAttr('seekThumb')
    const left = group?.getAttr('seekLeft') as number | undefined
    const width = group?.getAttr('seekWidth') as number | undefined
    if (!group || !fill || !thumb || !left || !width) {
      return
    }
    // 相机缩放漂移后进度条需按屏幕像素重排(卡可停在任意缩放档位)
    const scale = group.getAbsoluteScale().x || 1
    if (Math.abs(scale - (group.getAttr('seekScale') as number)) > scale * 0.05) {
      layoutSeekBar(group)
    }
    const ratio = videoHover.progress()?.ratio ?? 0
    fill.width(ratio * width)
    thumb.x(left + ratio * width)
  }

  /** 进度条按屏幕像素恒定布局:轨道/滑块/命中区厚度不随相机缩放变化. */
  function layoutSeekBar(group: any) {
    const objW = group.getAttr('seekObjW') as number | undefined
    const objH = group.getAttr('seekObjH') as number | undefined
    const bar = group.getAttr('seekBar')
    const track = group.getAttr('seekTrack')
    const fill = group.getAttr('seekFill')
    const thumb = group.getAttr('seekThumb')
    const hit = group.getAttr('seekHit')
    if (!objW || !objH || !bar || !track || !fill || !thumb || !hit) {
      return
    }
    const scale = group.getAbsoluteScale().x || 1
    const trackH = 4 / scale
    const thumbR = 5 / scale
    const left = 8 / scale
    const width = Math.max(0, objW - left * 2)
    const y = objH - 5 / scale - trackH
    track.setAttrs({ x: left, y, width, height: trackH, cornerRadius: trackH / 2 })
    fill.setAttrs({ x: left, y, height: trackH, cornerRadius: trackH / 2 })
    thumb.setAttrs({ y: y + trackH / 2, radius: thumbR, shadowBlur: 4 / scale })
    hit.setAttrs({ x: 0, y: y - 11 / scale, width: objW, height: 22 / scale })
    group.setAttrs({ seekLeft: left, seekWidth: width, seekScale: scale })
  }

  /** 进度条命中区:按下暂停逐帧预览,按住拖动 seek,松手恢复播放;指针已离卡则按离开停止. */
  function wireSeekEvents(group: any, seekHit: any) {
    seekHit.on('mouseenter', () => {
      engine.stage.container().style.cursor = 'ew-resize'
    })
    seekHit.on('mouseleave', () => {
      if (!videoHover.isScrubbing()) {
        engine.stage.container().style.cursor = ''
      }
    })
    seekHit.on('click tap', (e: any) => {
      // 单击进度条只做 seek,不触发卡片选中
      e.cancelBubble = true
    })
    seekHit.on('mousedown touchstart', (e: any) => {
      e.cancelBubble = true
      if (videoHover.activeId() !== group.id()) {
        return
      }
      const stage = group.getStage()
      const scrubFromPointer = () => {
        const pointer = stage.getPointerPosition()
        if (!pointer) {
          return
        }
        const local = group.getAbsoluteTransform().copy().invert().point(pointer)
        const left = group.getAttr('seekLeft') as number
        const width = group.getAttr('seekWidth') as number
        if (!(width > 0)) {
          return
        }
        const ratio = Math.min(1, Math.max(0, (local.x - left) / width))
        videoHover.scrubTo(ratio * (videoHover.progress()?.duration ?? 0))
      }
      videoHover.beginScrub()
      scrubFromPointer()
      const onMove = () => scrubFromPointer()
      const onUp = () => {
        window.removeEventListener('pointermove', onMove)
        window.removeEventListener('pointerup', onUp)
        window.removeEventListener('pointercancel', onUp)
        engine.stage.container().style.cursor = ''
        if (videoHover.activeId() !== group.id()) {
          return
        }
        videoHover.endScrub()
        // 拖动期间短暂滑出卡片时 mouseleave 被抑制;松手时指针不在卡上则按离开处理
        const pointer = stage.getPointerPosition()
        const hit = pointer ? layer.getIntersection(pointer) : null
        const over = Boolean(hit && (hit === group || hit.isDescendantOf(group)))
        if (!over) {
          videoHover.stop(group.id())
        }
      }
      window.addEventListener('pointermove', onMove)
      window.addEventListener('pointerup', onUp)
      window.addEventListener('pointercancel', onUp)
    })
  }

  /** 主题强调色:生成进度边框与悬停播放进度条共用. */
  function selectionAccent(): string {
    const dark = document.documentElement.dataset.theme === 'dark'
    const style = getComputedStyle(document.documentElement)
    return (
      style.getPropertyValue(dark ? '--studio-selection' : '--studio-accent').trim() || '#4778e6'
    )
  }

  function addProgressBorder(obj: CanvasObj, group: any) {
    const dark = document.documentElement.dataset.theme === 'dark'
    const accent = selectionAccent()
    const radius = Math.min(14, obj.width / 2, obj.height / 2)
    const perimeter = 2 * (obj.width + obj.height) - 8 * radius + 2 * Math.PI * radius
    const effect = new Konva.Group({ listening: false, visible: false })
    effect.add(
      new Konva.Rect({
        width: obj.width,
        height: obj.height,
        cornerRadius: radius,
        fillLinearGradientStartPoint: { x: 0, y: 0 },
        fillLinearGradientEndPoint: { x: obj.width, y: obj.height },
        fillLinearGradientColorStops: dark
          ? [0, '#242831', 1, '#14171e']
          : [0, '#f8faff', 1, '#e8eef9'],
        stroke: dark ? '#424b5c' : '#c7d2e5',
        strokeWidth: 1,
        strokeScaleEnabled: false,
      }),
    )
    const stars = []
    const spacing = Math.max(24, Math.min(obj.width, obj.height) / 11)
    const columns = Math.max(1, Math.min(24, Math.floor(obj.width / spacing)))
    const rows = Math.max(1, Math.min(24, Math.floor(obj.height / spacing)))
    for (let row = 0; row < rows; row++) {
      for (let col = 0; col < columns; col++) {
        const phase = col * 0.48 + row * 0.62
        const star = new Konva.Star({
          x: ((col + 0.5) * obj.width) / columns,
          y: ((row + 0.5) * obj.height) / rows,
          numPoints: 4,
          innerRadius: 1.7,
          outerRadius: 4.5,
          fill: accent,
          opacity: 0.2 + 0.65 * ((1 + Math.sin(-phase)) / 2),
          listening: false,
          perfectDrawEnabled: false,
        })
        star.setAttr('wavePhase', phase)
        stars.push(star)
        effect.add(star)
      }
    }
    const trail = new Konva.Rect({
      width: obj.width,
      height: obj.height,
      cornerRadius: radius,
      listening: false,
      strokeWidth: 2.6,
      strokeScaleEnabled: false,
      lineCap: 'round',
      stroke: accent,
      shadowColor: accent,
      shadowBlur: dark ? 8 : 4,
      shadowOpacity: dark ? 0.6 : 0.3,
      dash: [perimeter * 0.45, perimeter * 0.55],
    })
    effect.add(trail)
    group.add(effect)
    group.setAttr('loadingEffect', effect)
    group.setAttr('loadingStars', stars)
    group.setAttr('progressTrail', trail)
    group.setAttr('progressPerimeter', perimeter)
  }

  // Canvas 不会自动解析 CSS 变量,主题切换时重新读取并刷新覆盖层。
  const selectionPalette = () => {
    const style = getComputedStyle(document.documentElement)
    return {
      stroke: style.getPropertyValue('--studio-selection').trim() || '#4778e6',
      fill: style.getPropertyValue('--studio-panel').trim() || boardFill(),
    }
  }
  let selectionColors = selectionPalette()

  /* ---------- 图片加载(缩略图优先) ---------- */

  function pickUrl(obj: CanvasObj): string | null {
    if (imageUrlsFor) {
      const urls = imageUrlsFor(obj)
      if (obj.kind === 'video') {
        return urls.original
      }
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

  function loadImage(url: string): HTMLImageElement {
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

  function buildGroup(obj: CanvasObj) {
    const group = new Konva.Group({
      id: obj.id,
      x: obj.x,
      y: obj.y,
      rotation: obj.rotation || 0,
      draggable: true,
    })
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
    if (obj.nodeDraft || obj.textCard) {
      const title = new Konva.Text({
        text: obj.name || (obj.kind === 'text' ? '文本' : obj.kind === 'video' ? '视频' : '图片'),
        x: 0,
        y: -28,
        fontSize: 14,
        fill: textInk(),
        listening: false,
      })
      group.add(title)
      group.setAttr('nodeTitle', title)
    }

    if (isEmptyMedia(obj)) {
      bg.setAttrs(placeholderSurface(obj, document.documentElement.dataset.theme === 'dark'))
      bg.cornerRadius(8)
      const icon = new Konva.Path({
        data:
          obj.kind === 'video'
            ? 'M2 2 H14 V14 H2 Z M6 5 L11 8 L6 11 Z'
            : 'M2 2 H14 V14 H2 Z M2 11 L6 7 L10 11 L12 9 L14 11 M10 5 L11 5',
        stroke: '#858585',
        strokeWidth: 1.2,
        listening: false,
      })
      group.add(icon)
      group.setAttr('nodeStatus', icon)
      const progressLabel = new Konva.Text({
        fontSize: 15,
        fill: textInk(),
        align: 'center',
        listening: false,
        visible: false,
      })
      group.add(progressLabel)
      group.setAttr('progressLabel', progressLabel)
      addProgressBorder(obj, group)

      group.setAttr('bgRect', bg)
    } else if (obj.kind === 'image' || obj.kind === 'video') {
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
        const duration = Number(obj.durationSec ?? (obj.lineage?.params as any)?.durationSec ?? 0)
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
        group.setAttr('playIcon', playIcon)
        // 悬停播放的底部进度条:轨道+填充+滑块;透明命中区上下溢出卡片,便于按住拖动逐帧预览
        // 具体尺寸由 layoutSeekBar 按屏幕像素恒定布局(不随相机缩放变细/变大)
        const seekLeft = 8
        const seekWidth = Math.max(0, obj.width - seekLeft * 2)
        const seekY = obj.height - 9
        const seekBar = new Konva.Group({ listening: true, visible: false })
        const seekTrack = new Konva.Rect({
          x: seekLeft,
          y: seekY,
          width: seekWidth,
          height: 4,
          cornerRadius: 2,
          fill: 'rgba(255,255,255,0.32)',
          listening: false,
          perfectDrawEnabled: false,
        })
        const seekFill = new Konva.Rect({
          x: seekLeft,
          y: seekY,
          width: 0,
          height: 4,
          cornerRadius: 2,
          fill: selectionAccent(),
          listening: false,
          perfectDrawEnabled: false,
        })
        const seekThumb = new Konva.Circle({
          x: seekLeft,
          y: seekY + 2,
          radius: 5,
          fill: '#fff',
          shadowColor: 'rgba(0,0,0,0.35)',
          shadowBlur: 4,
          listening: false,
          perfectDrawEnabled: false,
        })
        const seekHit = new Konva.Rect({
          x: 0,
          y: seekY - 9,
          width: obj.width,
          height: 22,
          fill: 'rgba(0,0,0,0)',
          perfectDrawEnabled: false,
        })
        seekBar.add(seekTrack, seekFill, seekThumb, seekHit)
        group.add(seekBar)
        group.setAttr('seekBar', seekBar)
        group.setAttr('seekTrack', seekTrack)
        group.setAttr('seekFill', seekFill)
        group.setAttr('seekThumb', seekThumb)
        group.setAttr('seekHit', seekHit)
        group.setAttr('seekObjW', obj.width)
        group.setAttr('seekObjH', obj.height)
        layoutSeekBar(group)
        wireSeekEvents(group, seekHit)
        // 拖动逐帧预览(暂停)时中央显示 ⏸,样式与封面态 ▶ 同基调
        const pauseIcon = new Konva.Group({ listening: false, visible: false })
        const pauseSize = Math.min(28, obj.height / 4)
        const pauseBarW = Math.max(3, pauseSize / 4)
        const pauseGap = Math.max(2, pauseSize / 5)
        for (const center of [-(pauseBarW + pauseGap) / 2, (pauseBarW + pauseGap) / 2]) {
          pauseIcon.add(
            new Konva.Rect({
              x: center - pauseBarW / 2,
              y: -pauseSize / 2,
              width: pauseBarW,
              height: pauseSize,
              cornerRadius: pauseBarW / 2,
              fill: '#fff',
            }),
          )
        }
        pauseIcon.position({ x: obj.width / 2, y: obj.height / 2 })
        group.add(pauseIcon)
        group.setAttr('pauseIcon', pauseIcon)
      }
    } else if (obj.kind === 'board') {
      // 画板 = 背景容器:不裁剪、不自动收养重叠对象(PRD §4.2)
      bg.fill(document.documentElement.dataset.theme === 'dark' ? '#1c1c1c' : boardFill())
      bg.cornerRadius(8)
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
      bg.fill(obj.textCard ? obj.backgroundColor || boardFill() : 'rgba(0,0,0,0)')
      bg.stroke(obj.textCard ? 'rgba(127,127,127,0.3)' : 'rgba(0,0,0,0)')
      if (obj.textCard) {
        bg.setAttrs(placeholderSurface(obj, document.documentElement.dataset.theme === 'dark'))
      }
      group.setAttr('bgRect', bg)
      const textNode = new Konva.Text({
        text: obj.text || (obj.textCard ? '双击编辑文本' : ''),
        ...(obj.textCard ? { x: 16, y: 16, width: obj.width - 32, height: obj.height - 32 } : {}),
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
    } else if (obj.kind === 'draft') {
      bg.fill(document.documentElement.dataset.theme === 'dark' ? '#1c1c1c' : boardFill())
      bg.cornerRadius(8)
      bg.stroke(textInk())
      bg.opacity(0.45)
      const label = new Konva.Text({
        text: obj.mediaType === 'video' ? '视频 · 待创建' : '图片 · 待创建',
        width: obj.width,
        y: obj.height / 2 - 8,
        align: 'center',
        fontSize: 16,
        fill: textInk(),
        listening: false,
      })
      group.add(label)
    } else if (obj.kind === 'placeholder') {
      bg.setAttrs(placeholderSurface(obj, document.documentElement.dataset.theme === 'dark'))
      addProgressBorder(obj, group)
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

  /** 方向标签(左上角小胶囊)。 */
  function directionChip(title: string, width: number) {
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

  /** 槽位序号标签。 */
  function slotChip(slot: number, width: number) {
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

  function cacheGroup(obj: CanvasObj, group: any) {
    if (!isCacheable(obj) || group.getAttr('caching') || videoHover.activeId() === obj.id) {
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
      if (videoHover.activeId() === id) {
        continue
      }
      if (ratio > 2 || ratio < 0.5) {
        group.clearCache()
        cachedAtScale.delete(id)
        cacheGroup(obj, group)
        budget--
      }
    }
  }

  /* ---------- 对象事件:选择 / 拖动 ---------- */

  function wireGroupEvents(group: any) {
    group.on('mousedown touchstart', (e: { evt: any }) => {
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
      videoHover.stop()
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
      const main = dragStarts.find((d) => d.id === draggingId)
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
    group.on('mouseenter', (e: { evt?: { pointerType?: string } }) => {
      const o = nodeById(group.id())
      if (
        o?.kind === 'video' &&
        !isEmptyMedia(o) &&
        shouldStartVideoHover({
          pointerType: e.evt?.pointerType,
          hoverFine: hoverFinePointer(),
        })
      ) {
        const url = pickUrl(o)
        if (url) {
          let video = videoCache.get(url)
          if (!video) {
            video = document.createElement('video')
            video.preload = 'auto'
            video.muted = true
            video.playsInline = true
            video.src = url
            videoCache.set(url, video)
          }
          startHoverPreview(o.id, url, video)
        }
      }
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
      btn.on('click tap', (ev: any) => {
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
      // 拖动进度条可能短暂滑出卡片,预览去留推迟到松手时判定
      if (!videoHover.isScrubbing()) {
        videoHover.stop(group.id())
      }
      const btn = group.getAttr('cancelBtn')
      if (btn) {
        btn.destroy()
        group.setAttr('cancelBtn', null)
        layer.batchDraw()
      }
    })

    group.on('dragend', () => {
      if (cancelling) {
        return
      }
      if (!dragStarts) {
        return
      }
      const moves = dragStarts
        .filter((d) => d.node)
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
    stroke: selectionColors.stroke,
    strokeWidth: 1,
    strokeScaleEnabled: false,
    fill: selectionColors.stroke,
    fillEnabled: false,
    dash: [4, 3],
    visible: false,
    listening: false,
  })
  overlayLayer.add(marquee)

  // 空白处按下(target = Stage,不经过 Layer 冒泡)开始框选。
  // 移动/抬起监听 window:指针移出画布甚至窗口后松开也能正常收尾;
  // 会话序号保证残留的旧监听自废弃;平移手势(空格/中键)期间不启动。
  let marqueeSeq = 0
  let cancelMarquee = () => {}
  let cancelling = false
  engine.stage.on('mousedown touchstart', (e: { target: any; evt: any }) => {
    const button = e.evt.button
    if (
      e.target !== engine.stage ||
      (button !== undefined && button !== 0) ||
      !engine.interaction.canEdit()
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
      const box = marquee.getAttrs() as { x: number; y: number; width: number; height: number }
      if (box.width > 6 || box.height > 6) {
        selected.clear()
        for (const obj of Object.values(getDoc().objects)) {
          const o = { ...obj, ...transformBounds(obj) }
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
    cancelMarquee = () => {
      marqueeSeq++
      cleanup()
      marquee.visible(false)
    }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
    window.addEventListener('touchmove', move, { passive: true })
    window.addEventListener('touchend', up)
  })

  /* ---------- 选择呈现(覆盖层矩形,不碰缓存) ---------- */

  function syncSelectionRects() {
    syncSelectionRectPositions(selected, selectionRects, groups, overlayLayer)
    syncResizeHandles()
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
        rect.setAttrs({
          x: o.x,
          y: o.y,
          width: o.width,
          height: o.height,
          stroke: isEmptyMedia(o) ? '#929292' : selectionColors.stroke,
          rotation: o.rotation || 0,
        })
        continue
      }
      const created = new Konva.Rect({
        x: o.x,
        y: o.y,
        rotation: o.rotation || 0,
        width: o.width,
        height: o.height,
        stroke: isEmptyMedia(o) ? '#929292' : selectionColors.stroke,
        strokeWidth: 2,
        strokeScaleEnabled: false,
        cornerRadius: isEmptyMedia(o) ? 8 : 0,
        fillEnabled: false,
        listening: false,
      })
      selectionRects.set(id, created)
      overlayLayer.add(created)
    }
    syncResizeHandles()
    overlayLayer.batchDraw()
    onSelectionChange([...selected])
  }

  /* ---------- 八点缩放与中心旋转(屏幕尺寸固定) ---------- */

  const transformHandles = new Map<string, any>()
  let transforming: { id: string; before: Geometry; next: Geometry } | null = null
  const resizeAnchors: [number, number][] = [
    [0, 0],
    [1, 0],
    [1, 1],
    [0, 1],
    [0, 0.5],
    [1, 0.5],
    [0.5, 0],
    [0.5, 1],
  ]
  const rotateIndex = resizeAnchors.length

  function syncResizeHandles() {
    for (const [id, handles] of transformHandles) {
      if (!selected.has(id) || !nodeById(id) || isEmptyMedia(nodeById(id)) || selected.size !== 1) {
        handles.destroy()
        transformHandles.delete(id)
      }
    }
    for (const id of selected) {
      const obj = nodeById(id)
      if (
        !obj ||
        isEmptyMedia(obj) ||
        selected.size !== 1 ||
        !['image', 'video', 'board'].includes(obj.kind)
      ) {
        continue
      }
      let handles = transformHandles.get(id)
      if (!handles) {
        handles = new Konva.Group()
        handles.add(new Konva.Line({ listening: false, strokeWidth: 1, strokeScaleEnabled: false }))
        // 边线先添加，圆点置于其上；角点交叠处优先使用角点缩放。
        for (let index = 4; index < rotateIndex; index++) {
          const edge = new Konva.Line({
            draggable: true,
            strokeWidth: 2,
            strokeScaleEnabled: false,
            hitStrokeWidth: 'auto',
          })
          handles.add(edge)
          wireTransform(edge, id, index)
        }
        for (let index = 0; index <= rotateIndex; index++) {
          const handle = new Konva.Circle({
            draggable: true,
            strokeWidth: 2,
            strokeScaleEnabled: false,
          })
          handles.add(handle)
          wireTransform(handle, id, index)
        }
        transformHandles.set(id, handles)
        overlayLayer.add(handles)
      }
      const node = groups.get(id)
      const geometry =
        transforming?.id === id
          ? transforming.next
          : { ...obj, x: node?.x() ?? obj.x, y: node?.y() ?? obj.y }
      const scale = Math.max(0.05, engine.getCam().scale)
      handles.setAttrs({ x: geometry.x, y: geometry.y, rotation: geometry.rotation || 0 })
      const children = handles.getChildren()
      children[0].setAttrs({
        points: [geometry.width / 2, 0, geometry.width / 2, -28 / scale],
        stroke: selectionColors.stroke,
      })
      const edgePoints = [
        [0, 0, 0, geometry.height],
        [geometry.width, 0, geometry.width, geometry.height],
        [0, 0, geometry.width, 0],
        [0, geometry.height, geometry.width, geometry.height],
      ]
      edgePoints.forEach((points, index) => {
        children[index + 1].setAttrs({ x: 0, y: 0, points, stroke: selectionColors.stroke })
      })
      for (let index = 0; index <= rotateIndex; index++) {
        const point =
          index === rotateIndex
            ? { x: geometry.width / 2, y: -28 / scale }
            : {
                x: resizeAnchors[index][0] * geometry.width,
                y: resizeAnchors[index][1] * geometry.height,
              }
        // 命中范围与可见圆点（含描边）一致，避免圆点外的图片拖动被识别成缩放。
        children[index + 5].setAttrs({
          ...point,
          radius: 4.5 / scale,
          hitStrokeWidth: 'auto',
          fill: selectionColors.fill,
          stroke: selectionColors.stroke,
        })
      }
      handles.moveToTop()
    }
    overlayLayer.batchDraw()
  }

  function wireTransform(handle: any, id: string, index: number) {
    handle.on('mouseenter', () => {
      engine.stage.container().style.cursor =
        index === rotateIndex
          ? 'grab'
          : resizeCursor(
              resizeAnchors[index][0],
              resizeAnchors[index][1],
              nodeById(id)?.rotation || 0,
            )
    })
    handle.on('mouseleave', () => {
      engine.stage.container().style.cursor = ''
    })
    handle.on('mousedown touchstart', (event: any) => {
      event.cancelBubble = true
    })
    handle.on('dragstart', () => {
      videoHover.stop()
      const obj = nodeById(id)
      if (!obj) {
        return
      }
      const before = {
        x: obj.x,
        y: obj.y,
        width: obj.width,
        height: obj.height,
        rotation: obj.rotation || 0,
      }
      transforming = { id, before, next: before }
    })
    handle.on('dragmove', (event: any) => {
      if (!transforming || transforming.id !== id) {
        return
      }
      const { before } = transforming
      const pointer = engine.screenToWorld(engine.stage.getPointerPosition())
      if (index === rotateIndex) {
        const center = transformPoint(before, before.width / 2, before.height / 2)
        let rotation = (Math.atan2(pointer.y - center.y, pointer.x - center.x) * 180) / Math.PI + 90
        if (event.evt?.shiftKey) {
          rotation = Math.round(rotation / 15) * 15
        }
        transforming.next = rotateAroundCenter(before, rotation)
      } else {
        transforming.next = resizeFromCorner(
          before,
          pointer,
          resizeAnchors[index][0],
          resizeAnchors[index][1],
          index < 4 && Boolean(event.evt?.shiftKey),
        )
      }
      const next = transforming.next
      groups.get(id)?.setAttrs({
        x: next.x,
        y: next.y,
        rotation: next.rotation,
        scaleX: next.width / before.width,
        scaleY: next.height / before.height,
      })
      selectionRects.get(id)?.setAttrs(next)
      syncResizeHandles()
      layer.batchDraw()
    })
    handle.on('dragend', () => {
      if (cancelling) {
        return
      }
      if (!transforming || transforming.id !== id) {
        return
      }
      const { before, next } = transforming
      transforming = null
      groups.get(id)?.scale({ x: 1, y: 1 })
      engine.stage.container().style.cursor = ''
      const commands: CanvasCommand[] = []
      const obj = nodeById(id)
      // 未旋转的组图继续按右边缘的变化顺延,与尺寸变更一起撤销。
      const delta = next.x + next.width - before.x - before.width
      if (index !== rotateIndex && obj?.groupId && !before.rotation && delta) {
        const moves = Object.values(getDoc().objects)
          .filter((m) => m.id !== id && m.groupId === obj.groupId && m.x >= before.x + before.width)
          .map((m) => ({ id: m.id, from: { x: m.x, y: m.y }, to: { x: m.x + delta, y: m.y } }))
        if (moves.length) {
          commands.push(cmdMoveObjects(moves))
        }
      }
      if (
        Object.keys(next).some(
          (key) => next[key as keyof Geometry] !== before[key as keyof Geometry],
        )
      ) {
        commands.push(cmdUpdateObject(id, next, before))
      }
      if (commands.length) {
        options.applyCommand?.(commands.length > 1 ? cmdBatch(commands) : commands[0])
      }
      sync()
    })
  }

  /* ---------- 视口裁剪与生成动效 ---------- */

  function recomputeVisibility() {
    const cam = engine.getCam()
    const pad = MARGIN / cam.scale
    const minX = cam.x - pad
    const minY = cam.y - pad
    const maxX = cam.x + engine.stage.width() / cam.scale + pad
    const maxY = cam.y + engine.stage.height() / cam.scale + pad
    // 相机缩放后把手尺寸(屏幕恒定)需要重算
    if (transformHandles.size > 0) {
      syncResizeHandles()
    }
    let loadingVisible = false
    for (const [id, group] of groups) {
      const o = nodeById(id)
      if (!o) {
        continue
      }
      const bounds = transformBounds(o)
      const visible =
        bounds.x + bounds.width > minX &&
        bounds.x < maxX &&
        bounds.y + bounds.height > minY &&
        bounds.y < maxY
      group.visible(visible)
      if (!visible && videoHover.activeId() === id) {
        videoHover.stop(id)
      }
      if (visible && group.getAttr('loadingEffect')?.visible()) {
        loadingVisible = true
      }
    }
    if (loadingVisible && !reduceMotion.matches) {
      progressAnimation.start()
    } else {
      progressAnimation.stop()
    }
  }

  /* ---------- 组标题(PRD §4.2:点组标题选组,拖组标题移动整组) ---------- */

  /** groupId → 标题 Konva.Label */
  const titleNodes = new Map<string, any>()

  /** 每次同步重建组标题(组数量少,重建成本可忽略;成员边界实时计算)。 */
  function syncGroupTitles() {
    const doc = getDoc()
    const memberBounds = new Map<string, { minX: number; minY: number; maxX: number }>()
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
    const seen = new Set<string>()
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
   */
  function wireTitleEvents(node: any, groupId: string) {
    const memberIds = () =>
      Object.values(getDoc().objects)
        .filter((o) => o.groupId === groupId)
        .map((o) => o.id)

    node.on('mousedown touchstart', (e: any) => {
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

    let titleDrag: {
      from: Map<string, { x: number; y: number }>
      origin: { x: number; y: number }
    } | null = null
    node.on('dragstart', () => {
      videoHover.stop()
      const from = new Map<string, { x: number; y: number }>()
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
      if (cancelling) {
        titleDrag = null
        return
      }
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
    const seen = new Set<string>()
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
          group.getAttr('nodeAppearance') !== nodeAppearanceKey(obj) ||
          group.getAttr('builtW') !== obj.width ||
          group.getAttr('builtH') !== obj.height)
      ) {
        videoHover.stop(id)
        cancelPosterSeek(id)
        group.destroy()
        groups.delete(id)
        cachedAtScale.delete(id)
        group = null
      }
      if (!group) {
        group = buildGroup(obj)
        group.setAttr('builtKind', obj.kind)
        group.setAttr('nodeAppearance', nodeAppearanceKey(obj))
        group.setAttr('builtW', obj.width)
        group.setAttr('builtH', obj.height)
        groups.set(id, group)
        layer.add(group)
      }
      if (id !== draggingId) {
        group.position({ x: obj.x, y: obj.y })
        group.rotation(obj.rotation || 0)
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
        videoHover.stop(id)
        cancelPosterSeek(id)
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
    selectionColors = selectionPalette()
    marquee.setAttrs({ stroke: selectionColors.stroke, fill: selectionColors.stroke })
    videoHover.stop()
    hoverAnimation.stop()
    for (const id of posterSeekCancels.keys()) {
      cancelPosterSeek(id)
    }
    for (const [id, group] of groups) {
      group.destroy()
      groups.delete(id)
      cachedAtScale.delete(id)
    }
    sync()
  })

  /** 内容级刷新:图片 URL 按显示尺寸升级、占位进度文案、缓存触发。 */
  function refreshContent(obj: CanvasObj, group: any) {
    const isLoading =
      obj.kind === 'placeholder' ||
      (!!obj.nodeRun && ['submitting', 'queued', 'running'].includes(obj.nodeRun.status))
    group.getAttr('loadingEffect')?.visible(isLoading)
    const title = group.getAttr('nodeTitle')
    if (title) {
      title.text(obj.name || obj.kind)
      title.fill(textInk())
    }
    if (isEmptyMedia(obj)) {
      videoHover.stop(obj.id)
      title?.setAttrs({ fontSize: 12, y: -24 })
      const run = obj.nodeRun
      const loading = !!run && ['submitting', 'queued', 'running'].includes(run.status)
      group.getAttr('nodeStatus')?.visible(!run)
      group.getAttr('progressLabel')?.setAttrs({
        visible: !!run && !loading,
        text: nodeProgressLabel(obj),
        x: 12,
        y: obj.height / 2 + 8,
        width: Math.max(1, obj.width - 24),
        fill: textInk(),
      })
      group
        .getAttr('nodeStatus')
        ?.setAttrs({ x: obj.width / 2 - 8, y: obj.height / 2 - 8, scaleX: 1, scaleY: 1 })
      group.getAttr('nodeStatus')?.stroke('#858585')
      group
        .getAttr('bgRect')
        ?.setAttrs(placeholderSurface(obj, document.documentElement.dataset.theme === 'dark'))
      return
    }
    if (obj.kind === 'image' || obj.kind === 'video') {
      const imageNode = group.getChildren((n: any) => n.getClassName() === 'Image')[0]
      if (!imageNode) {
        return
      }
      const url = pickUrl(obj)
      const current = imageNode.image()
      if (
        obj.kind === 'video' &&
        shouldStopHoverForSourceChange({
          hovering: videoHover.activeId() === obj.id,
          activeHoverUrl,
          requestedUrl: url,
        })
      ) {
        videoHover.stop(obj.id)
      }
      if (url && (!current || current.src !== url)) {
        let img: HTMLImageElement | HTMLVideoElement
        if (obj.kind === 'video') {
          let video = videoCache.get(url)
          if (!video) {
            video = document.createElement('video')
            video.preload = 'auto'
            video.muted = true
            video.playsInline = true
            video.src = url
            videoCache.set(url, video)
          }
          img = video
        } else {
          img = loadImage(url)
        }
        const apply = () => {
          if (groups.get(obj.id) !== group || pickUrl(nodeById(obj.id)) !== url) {
            return
          }
          imageNode.image(img)
          cacheGroup(obj, group)
          layer.draw()
          if (
            obj.kind === 'video' &&
            img instanceof HTMLVideoElement &&
            videoHover.activeId() === obj.id
          ) {
            startHoverPreview(obj.id, url, img)
          }
        }
        if (
          img instanceof HTMLVideoElement
            ? img.readyState >= 2
            : img.complete && img.naturalWidth > 0
        ) {
          apply()
        } else {
          img.addEventListener(img instanceof HTMLVideoElement ? 'loadeddata' : 'load', apply, {
            once: true,
          })
        }
      } else if (current && !group.isCached() && videoHover.activeId() !== obj.id) {
        cacheGroup(obj, group)
      }
    } else if (obj.kind === 'error') {
      const detail = group.getChildren((n: any) => n.getClassName() === 'Text')[1]
      const text = obj.errorDetail || '未知原因'
      if (detail && detail.text() !== text) {
        detail.text(text)
      }
    } else if (obj.kind === 'text') {
      const textNode = group.getAttr('textNode')
      if (textNode) {
        if (textNode.text() !== (obj.text || (obj.textCard ? '双击编辑文本' : ''))) {
          textNode.text(obj.text || (obj.textCard ? '双击编辑文本' : ''))
        }
        if (!obj.fill && textNode.fill() !== textInk()) {
          textNode.fill(textInk())
        }
        group.width?.(Math.max(obj.width, textNode.width() || 0))
      }
      if (obj.textCard) {
        group
          .getAttr('bgRect')
          ?.setAttrs(placeholderSurface(obj, document.documentElement.dataset.theme === 'dark'))
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

  engine.interaction.subscribe((state) => {
    if (state.effective === 'pan' || state.gesture === 'pan') {
      videoHover.stop()
    }
    overlayLayer.listening(state.effective === 'select')
    overlayLayer.visible(state.effective === 'select')
    overlayLayer.draw()
  })
  const stopHoverIfHidden = () => {
    if (typeof document !== 'undefined' && document.hidden) {
      videoHover.stop()
    }
  }
  document.addEventListener('visibilitychange', stopHoverIfHidden)
  window.addEventListener('blur', () => {
    videoHover.stop()
  })
  engine.interaction.onCancel(() => {
    videoHover.stop()
    cancelling = true
    cancelMarquee()
    dragStarts = null
    draggingId = null
    transforming = null
    for (const node of [...groups.values(), ...titleNodes.values()]) {
      node.stopDrag()
    }
    for (const handles of transformHandles.values()) {
      for (const handle of handles.getChildren()) {
        handle.stopDrag()
      }
    }
    for (const [id, node] of groups) {
      const obj = nodeById(id)
      if (obj) {
        node.setAttrs({ x: obj.x, y: obj.y, rotation: obj.rotation || 0, scaleX: 1, scaleY: 1 })
      }
    }
    cancelling = false
    sync()
  })

  return {
    layer,
    overlayLayer,
    sync,
    getSelected: () => [...selected],
    recomputeVisibility,
    setSelected(ids: string[]) {
      selected.clear()
      for (const id of ids) {
        selected.add(id)
      }
      applySelection()
    },
    /** 删除当前选中(Delete 键/菜单);占位对象级联取消任务(PRD §5.4) */
    deleteSelected() {
      if (!engine.interaction.canEdit()) {
        return
      }
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
    /** 视野框住对象(生成落位后跟进) */
    revealObject(id: string) {
      const o = nodeById(id)
      if (!o) {
        return
      }
      // 布局未收敛(尺寸 0×0)时算出负比例会被钳到 5%,跳过等 fitToContent 统一处理
      if (engine.stage.width() <= 0 || engine.stage.height() <= 0) {
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
    /** 蒙版模式内容层不接收事件,按视觉层级做对象几何命中,不改变 listening。 */
    pickObjectAt(world: Point): string | null {
      for (const group of [...layer.getChildren()].reverse()) {
        const obj = nodeById(group.id())
        if (!obj || !group.visible()) {
          continue
        }
        const angle = ((obj.rotation || 0) * Math.PI) / 180
        const dx = world.x - obj.x,
          dy = world.y - obj.y
        const x = dx * Math.cos(angle) + dy * Math.sin(angle)
        const y = -dx * Math.sin(angle) + dy * Math.cos(angle)
        if (x >= 0 && y >= 0 && x <= obj.width && y <= obj.height) {
          return obj.id
        }
      }
      return null
    },
  }
}
