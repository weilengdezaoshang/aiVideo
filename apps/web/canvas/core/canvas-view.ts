import {
  cmdAddObjects,
  cmdRemoveObjects,
  type CanvasCommand,
  type CanvasObj,
} from '../state/commands.js'
import type { DocStore } from '../state/doc-store.js'
import { selectCanvasDocument } from '../state/generation-entity.js'
import { TypedEventHub } from './event-hub.js'
import {
  CanvasLayer,
  ConnectLayer,
  IntentLayer,
  InpaintLayer,
  LayerController,
  MaskLayer,
  ReadonlySelectLayer,
  SelectLayer,
  TextLayer,
} from './layers.js'
import type {
  CameraState,
  CanvasInputEvent,
  CanvasTool,
  CanvasViewEventMap,
  CanvasViewSnapshot,
  InteractionCancelReason,
  InteractionType,
  LayerContext,
  LayerOutput,
  ObjectPreview,
  Point,
  SelectionMode,
  ViewCapabilities,
} from './types.js'

const MIN_SCALE = 0.05
const MAX_SCALE = 8
const ZOOM_FACTOR = 1.25

export type CanvasViewOptions = {
  store: DocStore
  readonly?: boolean
  viewport?: { width: number; height: number }
  camera?: CameraState
  capabilities?: Partial<Omit<ViewCapabilities, 'canUndo' | 'canRedo'>>
}

export interface CanvasView {
  getSnapshot(): CanvasViewSnapshot
  subscribe(listener: () => void): () => void
  getDocument(): DocStore['doc']

  activateTool(tool: CanvasTool): void
  cancelCurrentOperation(reason?: InteractionCancelReason): void
  clearMaskDraft(): void

  select(ids: readonly string[], mode?: SelectionMode): void
  clearSelection(): void
  selectAll(): void
  deleteSelection(): void
  duplicateSelection(): void
  focusObject(id: string): void

  setViewportSize(width: number, height: number): void
  zoomIn(): void
  zoomOut(): void
  zoomTo(scale: number, anchor?: Point): void
  panBy(delta: Point): void
  fitToContent(): void
  centerOnObject(id: string): void
  screenToWorld(point: Point): Point
  worldToScreen(point: Point): Point

  undo(): boolean
  redo(): boolean
  execute(command: CanvasCommand): void

  dispatchInput(event: CanvasInputEvent): void
  on<K extends keyof CanvasViewEventMap>(
    type: K,
    handler: (payload: CanvasViewEventMap[K]) => void,
  ): () => void
  destroy(): void
}

export class CanvasViewImpl implements CanvasView {
  private readonly store: DocStore
  private readonly events = new TypedEventHub<CanvasViewEventMap>()
  private readonly layers = new LayerController()
  private readonly listeners = new Set<() => void>()
  private readonly configuredCapabilities: Partial<Omit<ViewCapabilities, 'canUndo' | 'canRedo'>>
  private unsubscribeStore: (() => boolean) | null
  private destroyed = false
  private camera: CameraState
  private viewport: { width: number; height: number }
  private selection: string[] = []
  private hoveredId: string | null = null
  private requestedTool: CanvasTool = 'select'
  private activeInteraction: InteractionType | null = null
  private objectPreviews: Record<string, ObjectPreview> = {}
  private marquee: CanvasViewSnapshot['marquee'] = null
  private connectionPreview: CanvasViewSnapshot['connectionPreview'] = null
  private maskStroke: CanvasViewSnapshot['maskStroke'] = null
  private maskStrokes: Array<readonly Point[]> = []
  private inpaintRegion: CanvasViewSnapshot['inpaintRegion'] = null
  private spacePressed = false
  private snapshot: CanvasViewSnapshot

  constructor(options: CanvasViewOptions) {
    this.store = options.store
    this.camera = options.camera ?? { x: 0, y: 0, scale: 1 }
    this.viewport = options.viewport ?? { width: 1, height: 1 }
    this.configuredCapabilities = options.capabilities ?? {}

    this.layers.register(new CanvasLayer())
    this.layers.register(new ReadonlySelectLayer())
    this.layers.register(new SelectLayer())
    this.layers.register(new ConnectLayer())
    this.layers.register(new TextLayer())
    this.layers.register(new MaskLayer())
    this.layers.register(new InpaintLayer())
    this.layers.register(new IntentLayer('recognizing'))
    this.layers.setReadonly(options.readonly === true)
    this.snapshot = this.buildSnapshot()
    this.unsubscribeStore = this.store.subscribe(() => {
      this.selection = this.selection.filter((id) => Boolean(this.store.doc.objects[id]))
      this.publishSnapshot()
    })
  }

  getSnapshot = (): CanvasViewSnapshot => this.snapshot

  getDocument(): DocStore['doc'] {
    return selectCanvasDocument(this.store.doc)
  }

  subscribe = (listener: () => void): (() => void) => {
    if (this.destroyed) {
      return () => {}
    }
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  on<K extends keyof CanvasViewEventMap>(
    type: K,
    handler: (payload: CanvasViewEventMap[K]) => void,
  ): () => void {
    return this.events.on(type, handler)
  }

  activateTool(tool: CanvasTool): void {
    if (tool === this.requestedTool) {
      return
    }
    this.cancelCurrentOperation('mode-change')
    if (this.requestedTool === 'mask' && tool !== 'mask') {
      this.maskStroke = null
      this.maskStrokes = []
    }
    if (!this.canActivate(tool)) {
      return
    }
    this.requestedTool = tool
    this.layers.activateTool(tool)
    this.publishSnapshot()
  }

  cancelCurrentOperation(reason: InteractionCancelReason = 'escape'): void {
    const mono = this.layers.cancel(reason, this.createContext())
    if (mono) {
      this.events.emit('interaction:cancelled', { type: mono.interaction, reason })
    }
    this.activeInteraction = null
    this.publishSnapshot()
  }

  clearMaskDraft(): void {
    this.maskStroke = null
    this.maskStrokes = []
    this.publishSnapshot()
  }

  select(ids: readonly string[], mode: SelectionMode = 'replace'): void {
    const valid = ids.filter((id) => Boolean(this.store.doc.objects[id]))
    if (mode === 'replace') {
      this.selection = [...new Set(valid)]
    }
    if (mode === 'append') {
      this.selection = [...new Set([...this.selection, ...valid])]
    }
    if (mode === 'toggle') {
      const next = new Set(this.selection)
      for (const id of valid) {
        if (next.has(id)) {
          next.delete(id)
        } else {
          next.add(id)
        }
      }
      this.selection = [...next]
    }
    this.publishSnapshot()
  }

  clearSelection(): void {
    this.select([])
  }

  selectAll(): void {
    this.select(this.store.doc.order)
  }

  deleteSelection(): void {
    if (!this.snapshot.capabilities.canDelete || this.selection.length === 0) {
      return
    }
    const entries = this.selection.flatMap((id) => {
      const object = this.store.doc.objects[id]
      return object ? [{ object, orderIndex: this.store.doc.order.indexOf(id) }] : []
    })
    if (entries.length === 0) {
      return
    }
    this.store.apply(cmdRemoveObjects(entries))
    this.selection = []
    this.publishSnapshot()
  }

  duplicateSelection(): void {
    if (!this.snapshot.capabilities.canEdit || this.selection.length === 0) {
      return
    }
    const copies = this.selection.flatMap((id) => {
      const source = this.store.doc.objects[id]
      if (!source) {
        return []
      }
      return [
        {
          ...structuredClone(source),
          id: `obj-${crypto.randomUUID()}`,
          x: source.x + 24,
          y: source.y + 24,
        },
      ]
    })
    if (copies.length === 0) {
      return
    }
    this.store.apply(cmdAddObjects(copies))
    this.selection = copies.map((copy) => copy.id)
    this.publishSnapshot()
  }

  focusObject(id: string): void {
    this.select([id])
    this.centerOnObject(id)
    this.events.emit('inspector:requested', { objectIds: [id] })
  }

  setViewportSize(width: number, height: number): void {
    const next = { width: Math.max(1, width), height: Math.max(1, height) }
    if (next.width === this.viewport.width && next.height === this.viewport.height) {
      return
    }
    this.viewport = next
    this.publishSnapshot()
  }

  zoomIn(): void {
    this.zoomTo(this.camera.scale * ZOOM_FACTOR)
  }

  zoomOut(): void {
    this.zoomTo(this.camera.scale / ZOOM_FACTOR)
  }

  zoomTo(
    scale: number,
    anchor: Point = { x: this.viewport.width / 2, y: this.viewport.height / 2 },
  ): void {
    const nextScale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale))
    const world = this.screenToWorld(anchor)
    this.camera = {
      x: world.x - anchor.x / nextScale,
      y: world.y - anchor.y / nextScale,
      scale: nextScale,
    }
    this.publishSnapshot()
  }

  panBy(delta: Point): void {
    this.camera = {
      ...this.camera,
      x: this.camera.x - delta.x / this.camera.scale,
      y: this.camera.y - delta.y / this.camera.scale,
    }
    this.publishSnapshot()
  }

  fitToContent(): void {
    const objects = this.store.doc.order.flatMap((id) => {
      const object = this.store.doc.objects[id]
      return object ? [object] : []
    })
    if (objects.length === 0) {
      this.camera = { x: -this.viewport.width / 2, y: -this.viewport.height / 2, scale: 1 }
      this.publishSnapshot()
      return
    }
    const bounds = objectBounds(objects)
    const padding = 80
    const scale = Math.min(
      1,
      Math.max(
        MIN_SCALE,
        Math.min(
          (this.viewport.width - padding * 2) / Math.max(1, bounds.width),
          (this.viewport.height - padding * 2) / Math.max(1, bounds.height),
        ),
      ),
    )
    this.camera = {
      x: bounds.x + bounds.width / 2 - this.viewport.width / 2 / scale,
      y: bounds.y + bounds.height / 2 - this.viewport.height / 2 / scale,
      scale,
    }
    this.publishSnapshot()
  }

  centerOnObject(id: string): void {
    const object = this.store.doc.objects[id]
    if (!object) {
      return
    }
    this.camera = {
      ...this.camera,
      x: object.x + object.width / 2 - this.viewport.width / 2 / this.camera.scale,
      y: object.y + object.height / 2 - this.viewport.height / 2 / this.camera.scale,
    }
    this.publishSnapshot()
  }

  screenToWorld(point: Point): Point {
    return {
      x: this.camera.x + point.x / this.camera.scale,
      y: this.camera.y + point.y / this.camera.scale,
    }
  }

  worldToScreen(point: Point): Point {
    return {
      x: (point.x - this.camera.x) * this.camera.scale,
      y: (point.y - this.camera.y) * this.camera.scale,
    }
  }

  undo(): boolean {
    const changed = this.store.undo()
    if (changed) {
      this.publishSnapshot()
    }
    return changed
  }

  redo(): boolean {
    const changed = this.store.redo()
    if (changed) {
      this.publishSnapshot()
    }
    return changed
  }

  execute(command: CanvasCommand): void {
    if (!this.snapshot.capabilities.canEdit) {
      return
    }
    this.store.apply(command)
  }

  dispatchInput(event: CanvasInputEvent): void {
    if (this.destroyed) {
      return
    }
    if (event.type === 'key-down') {
      const command = event.modifiers.meta || event.modifiers.ctrl
      if (command && event.key.toLowerCase() === 'z') {
        if (event.modifiers.shift) {
          this.redo()
        } else {
          this.undo()
        }
        return
      }
      if (command && event.key.toLowerCase() === 'a') {
        this.selectAll()
        return
      }
      if (command && event.key.toLowerCase() === 'd') {
        this.duplicateSelection()
        return
      }
      if (event.code === 'Space' && !this.spacePressed) {
        this.spacePressed = true
        this.layers.pushTemporary('canvas')
        this.publishSnapshot()
        return
      }
      if (event.key === 'Escape') {
        if (this.requestedTool === 'mask') {
          this.clearMaskDraft()
        }
        this.cancelCurrentOperation('escape')
        if (this.requestedTool !== 'select') {
          this.requestedTool = 'select'
          this.layers.activateTool('select')
          this.publishSnapshot()
        }
        return
      }
      if (event.key === 'Delete' || event.key === 'Backspace') {
        this.deleteSelection()
        return
      }
    }
    if (event.type === 'key-up' && event.code === 'Space') {
      this.spacePressed = false
      this.layers.popTemporary('canvas')
      this.publishSnapshot()
      return
    }
    const normalized =
      'screenPoint' in event && this.spacePressed
        ? { ...event, modifiers: { ...event.modifiers, space: true } }
        : event
    const result = this.layers.dispatch(normalized, this.createContext())
    if (result.type === 'capture') {
      this.activeInteraction = result.mono.interaction
    }
    if (result.type === 'complete') {
      if (result.output) {
        this.handleLayerOutput(result.output)
      }
      const interaction = this.activeInteraction
      const objectIds = this.selection
      this.activeInteraction = null
      if (interaction) {
        this.events.emit('interaction:committed', { type: interaction, objectIds })
      }
    }
    if (result.type === 'cancel') {
      const interaction = this.activeInteraction
      this.activeInteraction = null
      if (interaction) {
        this.events.emit('interaction:cancelled', { type: interaction, reason: result.reason })
      }
    }
    this.publishSnapshot()
  }

  destroy(): void {
    if (this.destroyed) {
      return
    }
    this.cancelCurrentOperation('destroyed')
    this.destroyed = true
    this.unsubscribeStore?.()
    this.unsubscribeStore = null
    this.listeners.clear()
    this.events.destroy()
  }

  private canActivate(tool: CanvasTool): boolean {
    const capabilities = this.capabilities()
    const allowed =
      tool === 'select' ||
      tool === 'pan' ||
      (tool === 'connect' && capabilities.canConnect) ||
      (tool === 'mask' && capabilities.canUseMask) ||
      (tool === 'inpaint' && capabilities.canUseMask) ||
      (tool === 'text' && capabilities.canEdit) ||
      tool === 'recognizing'
    if (!allowed) {
      this.events.emit('feedback:requested', { level: 'warning', message: '当前视图不允许此操作' })
    }
    return allowed
  }

  private createContext(): LayerContext {
    return {
      document: this.store.doc,
      selection: { ids: this.selection },
      camera: this.camera,
      viewport: this.viewport,
      capabilities: this.capabilities(),
      emit: (output) => this.handleLayerOutput(output),
    }
  }

  private handleLayerOutput(output: LayerOutput): void {
    switch (output.type) {
      case 'command-requested':
        if (this.capabilities().canEdit) {
          this.store.apply(output.command)
        }
        return
      case 'selection-changed':
        this.selection = [...new Set(output.objectIds.filter((id) => this.store.doc.objects[id]))]
        return
      case 'hover-changed':
        this.hoveredId = output.objectId
        return
      case 'camera-changed':
        this.camera = {
          ...output.camera,
          scale: Math.min(MAX_SCALE, Math.max(MIN_SCALE, output.camera.scale)),
        }
        return
      case 'preview-changed': {
        const previews = { ...this.objectPreviews }
        for (const id of output.objectIds) {
          if (output.preview) {
            previews[id] = { ...(previews[id] ?? {}), ...output.preview }
          } else {
            delete previews[id]
          }
        }
        this.objectPreviews = previews
        return
      }
      case 'marquee-changed':
        this.marquee = output.rect
        return
      case 'connection-preview-changed':
        this.connectionPreview = output.preview
        return
      case 'connection-requested':
        this.events.emit('connection:requested', {
          sourceId: output.sourceId,
          targetId: output.targetId,
        })
        return
      case 'mask-stroke-changed':
        this.maskStroke = output.points ? [...output.points] : null
        return
      case 'mask-stroke-committed':
        this.maskStrokes = [...this.maskStrokes, [...output.points]]
        this.events.emit('mask:stroke-committed', {
          sourceId: output.sourceId,
          points: output.points,
        })
        return
      case 'inpaint-region-changed':
        this.inpaintRegion = output.region
        return
      case 'inpaint-region-committed':
        this.events.emit('inpaint:region-committed', {
          sourceId: output.sourceId,
          region: output.region,
        })
        return
      case 'context-menu-requested':
        this.events.emit('context-menu:requested', {
          target: output.target,
          screenPoint: output.screenPoint,
          worldPoint: output.worldPoint,
        })
        return
      case 'text-edit-requested':
        this.events.emit('text-edit:requested', {
          objectId: output.objectId,
          screenRect: output.screenRect,
        })
        return
      case 'inspector-requested':
        this.events.emit('inspector:requested', { objectIds: output.objectIds })
        return
      case 'asset-picker-requested':
        this.events.emit('asset-picker:requested', { anchor: output.anchor })
        return
      case 'feedback-requested':
        this.events.emit('feedback:requested', { level: output.level, message: output.message })
    }
  }

  private capabilities(): ViewCapabilities {
    const edit = this.layers.getState().base !== 'readonly-select'
    return {
      canEdit: this.configuredCapabilities.canEdit ?? edit,
      canDelete: this.configuredCapabilities.canDelete ?? edit,
      canTransform: this.configuredCapabilities.canTransform ?? edit,
      canConnect: this.configuredCapabilities.canConnect ?? edit,
      canGenerate: this.configuredCapabilities.canGenerate ?? edit,
      canUseMask: this.configuredCapabilities.canUseMask ?? edit,
      canUndo: this.store.canUndo(),
      canRedo: this.store.canRedo(),
    }
  }

  private buildSnapshot(): CanvasViewSnapshot {
    return Object.freeze({
      camera: Object.freeze({ ...this.camera }),
      viewport: Object.freeze({ ...this.viewport }),
      selection: Object.freeze([...this.selection]),
      hoveredId: this.hoveredId,
      requestedTool: this.requestedTool,
      effectiveLayer: this.layers.effectiveLayerId(),
      activeInteraction: this.activeInteraction,
      capabilities: Object.freeze(this.capabilities()),
      objectPreviews: Object.freeze({ ...this.objectPreviews }),
      marquee: this.marquee ? Object.freeze({ ...this.marquee }) : null,
      connectionPreview: this.connectionPreview
        ? Object.freeze({ ...this.connectionPreview })
        : null,
      maskStroke: this.maskStroke ? Object.freeze([...this.maskStroke]) : null,
      maskStrokes: Object.freeze(this.maskStrokes.map((stroke) => Object.freeze([...stroke]))),
      inpaintRegion: this.inpaintRegion ? Object.freeze({ ...this.inpaintRegion }) : null,
    })
  }

  private publishSnapshot(): void {
    if (this.destroyed) {
      return
    }
    this.snapshot = this.buildSnapshot()
    for (const listener of [...this.listeners]) {
      listener()
    }
  }
}

export function createCanvasView(options: CanvasViewOptions): CanvasView {
  return new CanvasViewImpl(options)
}

function objectBounds(objects: readonly CanvasObj[]) {
  const left = Math.min(...objects.map((object) => object.x))
  const top = Math.min(...objects.map((object) => object.y))
  const right = Math.max(...objects.map((object) => object.x + object.width))
  const bottom = Math.max(...objects.map((object) => object.y + object.height))
  return { x: left, y: top, width: right - left, height: bottom - top }
}
