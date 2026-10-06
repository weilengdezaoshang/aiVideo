import type { CanvasCommand, CanvasObj } from '../state/commands.js'
import type { CanvasDocData } from '../state/doc-store.js'

export type Point = { x: number; y: number }
export type Rect = { x: number; y: number; width: number; height: number }
export type CameraState = { x: number; y: number; scale: number }

export type CanvasTool = 'select' | 'pan' | 'connect' | 'text' | 'mask' | 'inpaint' | 'recognizing'

export type LayerId =
  'canvas' | 'readonly-select' | 'select' | 'connect' | 'text' | 'mask' | 'inpaint' | 'recognizing'

export type InteractionType =
  | 'select'
  | 'marquee'
  | 'move'
  | 'resize'
  | 'rotate'
  | 'connect'
  | 'pan'
  | 'text-create'
  | 'mask-paint'
  | 'inpaint-region'

export type InteractionCancelReason =
  'escape' | 'mode-change' | 'object-removed' | 'destroyed' | 'pointer-cancel'

export type SelectionMode = 'replace' | 'append' | 'toggle'
export type ResizeHandle = 'nw' | 'n' | 'ne' | 'e' | 'se' | 's' | 'sw' | 'w'

export type CanvasHitTarget =
  | { kind: 'canvas' }
  | { kind: 'object'; objectId: string }
  | { kind: 'resize-handle'; objectId: string; handle: ResizeHandle }
  | { kind: 'rotate-handle'; objectId: string }
  | { kind: 'port'; objectId: string; port: 'input' | 'output' }

export type CanvasMouseEventType =
  | 'lbutton-down'
  | 'lbutton-up'
  | 'rbutton-down'
  | 'rbutton-up'
  | 'move'
  | 'enter'
  | 'leave'
  | 'double-click'
  | 'context-menu'
  | 'wheel'
  | 'pointer-cancel'

export type CanvasModifiers = {
  shift: boolean
  alt: boolean
  ctrl: boolean
  meta: boolean
  space: boolean
}

export type CanvasMouseEvent = {
  type: CanvasMouseEventType
  pointerId: number
  button: number
  screenPoint: Point
  worldPoint: Point
  modifiers: CanvasModifiers
  target: CanvasHitTarget | null
  wheelDelta?: Point
}

export type CanvasKeyboardEvent = {
  type: 'key-down' | 'key-up'
  key: string
  code: string
  modifiers: CanvasModifiers
}

export type CanvasInputEvent = CanvasMouseEvent | CanvasKeyboardEvent

export type ViewCapabilities = {
  canEdit: boolean
  canDelete: boolean
  canTransform: boolean
  canConnect: boolean
  canGenerate: boolean
  canUseMask: boolean
  canUndo: boolean
  canRedo: boolean
}

export type ObjectPreview = Partial<Pick<CanvasObj, 'x' | 'y' | 'width' | 'height' | 'rotation'>>

export type CanvasViewSnapshot = {
  camera: CameraState
  viewport: { width: number; height: number }
  selection: readonly string[]
  hoveredId: string | null
  requestedTool: CanvasTool
  effectiveLayer: LayerId
  activeInteraction: InteractionType | null
  capabilities: ViewCapabilities
  objectPreviews: Readonly<Record<string, ObjectPreview>>
  marquee: Rect | null
  connectionPreview: { sourceId: string; start: Point; end: Point } | null
  maskStroke: readonly Point[] | null
  maskStrokes: readonly (readonly Point[])[]
  inpaintRegion: Rect | null
}

export type CanvasViewEventMap = {
  'context-menu:requested': {
    target: CanvasHitTarget | null
    screenPoint: Point
    worldPoint: Point
  }
  'text-edit:requested': { objectId: string; screenRect: Rect }
  'connection:requested': { sourceId: string; targetId: string }
  'mask:stroke-committed': { sourceId: string; points: readonly Point[] }
  'inpaint:region-committed': { sourceId: string; region: Rect }
  'inspector:requested': { objectIds: readonly string[] }
  'asset-picker:requested': { anchor: Point }
  'feedback:requested': { level: 'info' | 'warning' | 'error'; message: string }
  'interaction:committed': { type: InteractionType; objectIds: readonly string[] }
  'interaction:cancelled': { type: InteractionType; reason: InteractionCancelReason }
}

export type LayerOutput =
  | {
      type: 'command-requested'
      command: CanvasCommand
      interaction?: InteractionType
      objectIds?: readonly string[]
    }
  | { type: 'selection-changed'; objectIds: readonly string[] }
  | { type: 'hover-changed'; objectId: string | null }
  | { type: 'camera-changed'; camera: CameraState }
  | { type: 'preview-changed'; objectIds: readonly string[]; preview: ObjectPreview | null }
  | { type: 'marquee-changed'; rect: Rect | null }
  | {
      type: 'connection-preview-changed'
      preview: { sourceId: string; start: Point; end: Point } | null
    }
  | { type: 'connection-requested'; sourceId: string; targetId: string }
  | { type: 'mask-stroke-changed'; points: readonly Point[] | null }
  | { type: 'mask-stroke-committed'; sourceId: string; points: readonly Point[] }
  | { type: 'inpaint-region-changed'; region: Rect | null }
  | { type: 'inpaint-region-committed'; sourceId: string; region: Rect }
  | {
      type: 'context-menu-requested'
      target: CanvasHitTarget | null
      screenPoint: Point
      worldPoint: Point
    }
  | { type: 'text-edit-requested'; objectId: string; screenRect: Rect }
  | { type: 'inspector-requested'; objectIds: readonly string[] }
  | { type: 'asset-picker-requested'; anchor: Point }
  | { type: 'feedback-requested'; level: 'info' | 'warning' | 'error'; message: string }

export type SelectionSnapshot = { ids: readonly string[] }

export type LayerContext = {
  readonly document: CanvasDocData
  readonly selection: SelectionSnapshot
  readonly camera: CameraState
  readonly viewport: { width: number; height: number }
  readonly capabilities: ViewCapabilities
  emit(output: LayerOutput): void
}

export type EventResult =
  | { type: 'pass' }
  | { type: 'handled' }
  | { type: 'capture'; mono: EventMono }
  | { type: 'complete'; output?: LayerOutput }
  | { type: 'cancel'; reason: InteractionCancelReason }

export interface EventMono {
  readonly id: string
  readonly interaction: InteractionType
  readonly objectIds: readonly string[]
  onLButtonDown(event: CanvasMouseEvent, context: LayerContext): EventResult
  onLButtonMove(event: CanvasMouseEvent, context: LayerContext): EventResult
  onLButtonUp(event: CanvasMouseEvent, context: LayerContext): EventResult
  onRButtonDown?(event: CanvasMouseEvent, context: LayerContext): EventResult
  onRButtonUp?(event: CanvasMouseEvent, context: LayerContext): EventResult
  cancel(reason: InteractionCancelReason, context: LayerContext): void
}

export interface EventLayer {
  readonly id: LayerId
  hit(event: CanvasMouseEvent, context: LayerContext): EventMono | null
  onInput(event: CanvasInputEvent, context: LayerContext): EventResult
  onEnter?(context: LayerContext): void
  onLeave?(context: LayerContext): void
}

export type LayerState = {
  base: 'select' | 'readonly-select'
  tool: LayerId | null
  temporary: readonly LayerId[]
  modal: LayerId | null
  activeMono: string | null
}
