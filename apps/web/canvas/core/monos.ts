import { cmdMoveObjects, cmdUpdateObject, type CanvasObj } from '../state/commands.js'
import {
  angleBetween,
  normalizeRect,
  objectRect,
  rectIntersects,
  resizeFromHandle,
} from './geometry.js'
import type {
  CanvasMouseEvent,
  EventMono,
  EventResult,
  InteractionCancelReason,
  LayerContext,
  Point,
  ResizeHandle,
} from './types.js'

abstract class BaseMono implements EventMono {
  abstract readonly id: string
  abstract readonly interaction: EventMono['interaction']
  abstract readonly objectIds: readonly string[]

  onLButtonDown(_event: CanvasMouseEvent, _context: LayerContext): EventResult {
    return { type: 'handled' }
  }

  abstract onLButtonMove(event: CanvasMouseEvent, context: LayerContext): EventResult
  abstract onLButtonUp(event: CanvasMouseEvent, context: LayerContext): EventResult

  onRButtonDown(_event: CanvasMouseEvent, _context: LayerContext): EventResult {
    return { type: 'cancel', reason: 'escape' }
  }

  cancel(_reason: InteractionCancelReason, context: LayerContext): void {
    context.emit({ type: 'preview-changed', objectIds: this.objectIds, preview: null })
    context.emit({ type: 'marquee-changed', rect: null })
  }
}

export class MoveObjectsMono extends BaseMono {
  readonly id = 'move-objects'
  readonly interaction = 'move' as const
  readonly objectIds: readonly string[]
  private readonly start: Point
  private readonly origins: Map<string, Point>

  constructor(event: CanvasMouseEvent, objects: readonly CanvasObj[]) {
    super()
    this.start = event.worldPoint
    this.objectIds = objects.map((object) => object.id)
    this.origins = new Map(objects.map((object) => [object.id, { x: object.x, y: object.y }]))
  }

  onLButtonMove(event: CanvasMouseEvent, context: LayerContext): EventResult {
    const delta = { x: event.worldPoint.x - this.start.x, y: event.worldPoint.y - this.start.y }
    for (const id of this.objectIds) {
      const origin = this.origins.get(id)
      if (origin) {
        context.emit({
          type: 'preview-changed',
          objectIds: [id],
          preview: { x: origin.x + delta.x, y: origin.y + delta.y },
        })
      }
    }
    return { type: 'handled' }
  }

  onLButtonUp(event: CanvasMouseEvent, context: LayerContext): EventResult {
    const delta = { x: event.worldPoint.x - this.start.x, y: event.worldPoint.y - this.start.y }
    context.emit({ type: 'preview-changed', objectIds: this.objectIds, preview: null })
    if (delta.x === 0 && delta.y === 0) {
      return { type: 'complete' }
    }
    return {
      type: 'complete',
      output: {
        type: 'command-requested',
        interaction: 'move',
        objectIds: this.objectIds,
        command: cmdMoveObjects(
          this.objectIds.flatMap((id) => {
            const origin = this.origins.get(id)
            return origin
              ? [{ id, from: origin, to: { x: origin.x + delta.x, y: origin.y + delta.y } }]
              : []
          }),
        ),
      },
    }
  }
}

export class ResizeObjectMono extends BaseMono {
  readonly id = 'resize-object'
  readonly interaction = 'resize' as const
  readonly objectIds: readonly string[]
  private readonly start: Point
  private readonly origin: CanvasObj
  private readonly handle: ResizeHandle

  constructor(event: CanvasMouseEvent, object: CanvasObj, handle: ResizeHandle) {
    super()
    this.start = event.worldPoint
    this.origin = { ...object }
    this.objectIds = [object.id]
    this.handle = handle
  }

  private next(event: CanvasMouseEvent) {
    return resizeFromHandle(objectRect(this.origin), this.handle, {
      x: event.worldPoint.x - this.start.x,
      y: event.worldPoint.y - this.start.y,
    })
  }

  onLButtonMove(event: CanvasMouseEvent, context: LayerContext): EventResult {
    context.emit({ type: 'preview-changed', objectIds: this.objectIds, preview: this.next(event) })
    return { type: 'handled' }
  }

  onLButtonUp(event: CanvasMouseEvent, context: LayerContext): EventResult {
    const next = this.next(event)
    context.emit({ type: 'preview-changed', objectIds: this.objectIds, preview: null })
    return {
      type: 'complete',
      output: {
        type: 'command-requested',
        interaction: 'resize',
        objectIds: this.objectIds,
        command: cmdUpdateObject(this.origin.id, next, objectRect(this.origin)),
      },
    }
  }
}

export class RotateObjectMono extends BaseMono {
  readonly id = 'rotate-object'
  readonly interaction = 'rotate' as const
  readonly objectIds: readonly string[]
  private readonly origin: CanvasObj
  private readonly startAngle: number
  private readonly originRotation: number

  constructor(event: CanvasMouseEvent, object: CanvasObj) {
    super()
    this.origin = { ...object }
    this.objectIds = [object.id]
    const center = { x: object.x + object.width / 2, y: object.y + object.height / 2 }
    this.startAngle = angleBetween(center, event.worldPoint)
    this.originRotation = object.rotation ?? 0
  }

  private rotation(event: CanvasMouseEvent): number {
    const center = {
      x: this.origin.x + this.origin.width / 2,
      y: this.origin.y + this.origin.height / 2,
    }
    return this.originRotation + angleBetween(center, event.worldPoint) - this.startAngle
  }

  onLButtonMove(event: CanvasMouseEvent, context: LayerContext): EventResult {
    context.emit({
      type: 'preview-changed',
      objectIds: this.objectIds,
      preview: { rotation: this.rotation(event) },
    })
    return { type: 'handled' }
  }

  onLButtonUp(event: CanvasMouseEvent, context: LayerContext): EventResult {
    const rotation = this.rotation(event)
    context.emit({ type: 'preview-changed', objectIds: this.objectIds, preview: null })
    return {
      type: 'complete',
      output: {
        type: 'command-requested',
        interaction: 'rotate',
        objectIds: this.objectIds,
        command: cmdUpdateObject(this.origin.id, { rotation }, { rotation: this.origin.rotation }),
      },
    }
  }
}

export class MarqueeSelectMono extends BaseMono {
  readonly id = 'marquee-select'
  readonly interaction = 'marquee' as const
  readonly objectIds: readonly string[] = []
  private readonly start: Point

  constructor(event: CanvasMouseEvent) {
    super()
    this.start = event.worldPoint
  }

  onLButtonMove(event: CanvasMouseEvent, context: LayerContext): EventResult {
    context.emit({ type: 'marquee-changed', rect: normalizeRect(this.start, event.worldPoint) })
    return { type: 'handled' }
  }

  onLButtonUp(event: CanvasMouseEvent, context: LayerContext): EventResult {
    const rect = normalizeRect(this.start, event.worldPoint)
    const ids = context.document.order.filter((id) => {
      const object = context.document.objects[id]
      return object ? rectIntersects(rect, objectRect(object)) : false
    })
    context.emit({ type: 'marquee-changed', rect: null })
    context.emit({ type: 'selection-changed', objectIds: ids })
    return { type: 'complete' }
  }
}

export class PanMono extends BaseMono {
  readonly id = 'pan'
  readonly interaction = 'pan' as const
  readonly objectIds: readonly string[] = []
  private readonly start: Point
  private readonly originCamera: LayerContext['camera']

  constructor(event: CanvasMouseEvent, camera: LayerContext['camera']) {
    super()
    this.start = event.screenPoint
    this.originCamera = { ...camera }
  }

  onLButtonMove(event: CanvasMouseEvent, context: LayerContext): EventResult {
    context.emit({
      type: 'camera-changed',
      camera: {
        ...this.originCamera,
        x: this.originCamera.x - (event.screenPoint.x - this.start.x) / this.originCamera.scale,
        y: this.originCamera.y - (event.screenPoint.y - this.start.y) / this.originCamera.scale,
      },
    })
    return { type: 'handled' }
  }

  onLButtonUp(_event: CanvasMouseEvent, _context: LayerContext): EventResult {
    return { type: 'complete' }
  }
}

export class ConnectMono extends BaseMono {
  readonly id = 'connect'
  readonly interaction = 'connect' as const
  readonly objectIds: readonly string[]
  private readonly sourceId: string
  private readonly start: Point

  constructor(event: CanvasMouseEvent, sourceId: string) {
    super()
    this.sourceId = sourceId
    this.objectIds = [sourceId]
    this.start = event.worldPoint
  }

  override onLButtonDown(_event: CanvasMouseEvent, context: LayerContext): EventResult {
    context.emit({
      type: 'connection-preview-changed',
      preview: { sourceId: this.sourceId, start: this.start, end: this.start },
    })
    return { type: 'handled' }
  }

  onLButtonMove(event: CanvasMouseEvent, context: LayerContext): EventResult {
    context.emit({
      type: 'connection-preview-changed',
      preview: { sourceId: this.sourceId, start: this.start, end: event.worldPoint },
    })
    return { type: 'handled' }
  }

  onLButtonUp(event: CanvasMouseEvent, context: LayerContext): EventResult {
    context.emit({ type: 'connection-preview-changed', preview: null })
    const targetId =
      event.target?.kind === 'port' && event.target.port === 'input' ? event.target.objectId : null
    if (!targetId || targetId === this.sourceId) {
      return { type: 'cancel', reason: 'pointer-cancel' }
    }
    context.emit({ type: 'connection-requested', sourceId: this.sourceId, targetId })
    return { type: 'complete' }
  }

  override cancel(reason: InteractionCancelReason, context: LayerContext): void {
    super.cancel(reason, context)
    context.emit({ type: 'connection-preview-changed', preview: null })
  }
}

export class TextCreateMono extends BaseMono {
  readonly id = 'text-create'
  readonly interaction = 'text-create' as const
  readonly objectIds: readonly string[]
  private readonly start: Point
  private readonly objectId: string

  constructor(event: CanvasMouseEvent, objectId = `text-${crypto.randomUUID()}`) {
    super()
    this.start = event.worldPoint
    this.objectId = objectId
    this.objectIds = [objectId]
  }

  onLButtonMove(event: CanvasMouseEvent, context: LayerContext): EventResult {
    context.emit({ type: 'marquee-changed', rect: normalizeRect(this.start, event.worldPoint) })
    return { type: 'handled' }
  }

  onLButtonUp(event: CanvasMouseEvent, context: LayerContext): EventResult {
    const drawn = normalizeRect(this.start, event.worldPoint)
    const rect = {
      x: drawn.x,
      y: drawn.y,
      width: Math.max(180, drawn.width),
      height: Math.max(72, drawn.height),
    }
    const object: CanvasObj = {
      id: this.objectId,
      kind: 'text',
      ...rect,
      text: '',
      textCard: true,
      fontSize: 16,
      backgroundColor: '#ffffff',
    }
    context.emit({ type: 'marquee-changed', rect: null })
    context.emit({
      type: 'command-requested',
      command: { type: 'addObjects', objects: [object] },
      interaction: 'text-create',
      objectIds: [object.id],
    })
    context.emit({ type: 'selection-changed', objectIds: [object.id] })
    context.emit({
      type: 'text-edit-requested',
      objectId: object.id,
      screenRect: {
        x: (rect.x - context.camera.x) * context.camera.scale,
        y: (rect.y - context.camera.y) * context.camera.scale,
        width: rect.width * context.camera.scale,
        height: rect.height * context.camera.scale,
      },
    })
    return { type: 'complete' }
  }
}

export class MaskPaintMono extends BaseMono {
  readonly id = 'mask-paint'
  readonly interaction = 'mask-paint' as const
  readonly objectIds: readonly string[]
  private readonly points: Point[]

  constructor(event: CanvasMouseEvent, objectId: string) {
    super()
    this.objectIds = [objectId]
    this.points = [event.worldPoint]
  }

  override onLButtonDown(_event: CanvasMouseEvent, context: LayerContext): EventResult {
    context.emit({ type: 'mask-stroke-changed', points: [...this.points] })
    return { type: 'handled' }
  }

  onLButtonMove(event: CanvasMouseEvent, context: LayerContext): EventResult {
    this.points.push(event.worldPoint)
    context.emit({ type: 'mask-stroke-changed', points: [...this.points] })
    return { type: 'handled' }
  }

  onLButtonUp(event: CanvasMouseEvent, context: LayerContext): EventResult {
    this.points.push(event.worldPoint)
    context.emit({ type: 'mask-stroke-changed', points: null })
    context.emit({
      type: 'mask-stroke-committed',
      sourceId: this.objectIds[0],
      points: [...this.points],
    })
    return { type: 'complete' }
  }

  override cancel(reason: InteractionCancelReason, context: LayerContext): void {
    super.cancel(reason, context)
    context.emit({ type: 'mask-stroke-changed', points: null })
  }
}

export class InpaintRegionMono extends BaseMono {
  readonly id = 'inpaint-region'
  readonly interaction = 'inpaint-region' as const
  readonly objectIds: readonly string[]
  private readonly start: Point

  constructor(event: CanvasMouseEvent, objectId: string) {
    super()
    this.objectIds = [objectId]
    this.start = event.worldPoint
  }

  onLButtonMove(event: CanvasMouseEvent, context: LayerContext): EventResult {
    context.emit({
      type: 'inpaint-region-changed',
      region: normalizeRect(this.start, event.worldPoint),
    })
    return { type: 'handled' }
  }

  onLButtonUp(event: CanvasMouseEvent, context: LayerContext): EventResult {
    const region = normalizeRect(this.start, event.worldPoint)
    context.emit({ type: 'inpaint-region-changed', region: null })
    if (region.width < 2 || region.height < 2) {
      return { type: 'cancel', reason: 'pointer-cancel' }
    }
    context.emit({ type: 'inpaint-region-committed', sourceId: this.objectIds[0], region })
    return { type: 'complete' }
  }

  override cancel(reason: InteractionCancelReason, context: LayerContext): void {
    super.cancel(reason, context)
    context.emit({ type: 'inpaint-region-changed', region: null })
  }
}
