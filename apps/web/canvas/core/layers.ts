import {
  ConnectMono,
  InpaintRegionMono,
  MarqueeSelectMono,
  MaskPaintMono,
  MoveObjectsMono,
  PanMono,
  ResizeObjectMono,
  RotateObjectMono,
  TextCreateMono,
} from './monos.js'
import type {
  CanvasInputEvent,
  CanvasMouseEvent,
  CanvasTool,
  EventLayer,
  EventMono,
  EventResult,
  InteractionCancelReason,
  LayerContext,
  LayerId,
  LayerState,
} from './types.js'

const PASS: EventResult = { type: 'pass' }
const HANDLED: EventResult = { type: 'handled' }

function isMouse(event: CanvasInputEvent): event is CanvasMouseEvent {
  return 'screenPoint' in event
}

export class CanvasLayer implements EventLayer {
  readonly id = 'canvas' as const

  hit(event: CanvasMouseEvent, context: LayerContext): EventMono | null {
    const wantsPan = event.button === 0 || event.button === 1 || event.modifiers.space
    return wantsPan ? new PanMono(event, context.camera) : null
  }

  onInput(event: CanvasInputEvent, context: LayerContext): EventResult {
    if (!isMouse(event)) {
      return PASS
    }
    if (event.type === 'wheel' && event.wheelDelta) {
      const isZoom =
        event.modifiers.ctrl || Math.abs(event.wheelDelta.y) >= Math.abs(event.wheelDelta.x)
      if (isZoom) {
        const factor = event.wheelDelta.y < 0 ? 1.1 : 1 / 1.1
        const scale = Math.min(8, Math.max(0.05, context.camera.scale * factor))
        const world = event.worldPoint
        context.emit({
          type: 'camera-changed',
          camera: {
            x: world.x - event.screenPoint.x / scale,
            y: world.y - event.screenPoint.y / scale,
            scale,
          },
        })
      } else {
        context.emit({
          type: 'camera-changed',
          camera: {
            ...context.camera,
            x: context.camera.x + event.wheelDelta.x / context.camera.scale,
            y: context.camera.y + event.wheelDelta.y / context.camera.scale,
          },
        })
      }
      return HANDLED
    }
    return PASS
  }
}

export class ReadonlySelectLayer implements EventLayer {
  readonly id: LayerId = 'readonly-select'

  hit(_event: CanvasMouseEvent, _context: LayerContext): EventMono | null {
    return null
  }

  onInput(event: CanvasInputEvent, context: LayerContext): EventResult {
    if (!isMouse(event)) {
      return PASS
    }
    if (event.type === 'move') {
      context.emit({
        type: 'hover-changed',
        objectId: event.target?.kind === 'object' ? event.target.objectId : null,
      })
      return PASS
    }
    if (event.type === 'lbutton-down') {
      const objectId = event.target?.kind === 'object' ? event.target.objectId : null
      context.emit({ type: 'selection-changed', objectIds: objectId ? [objectId] : [] })
      return HANDLED
    }
    if (event.type === 'context-menu' || event.type === 'rbutton-up') {
      context.emit({
        type: 'context-menu-requested',
        target: event.target,
        screenPoint: event.screenPoint,
        worldPoint: event.worldPoint,
      })
      return HANDLED
    }
    if (event.type === 'double-click' && event.target?.kind === 'object') {
      context.emit({ type: 'inspector-requested', objectIds: [event.target.objectId] })
      return HANDLED
    }
    return PASS
  }
}

export class SelectLayer extends ReadonlySelectLayer {
  override readonly id: LayerId = 'select'

  override hit(event: CanvasMouseEvent, context: LayerContext): EventMono | null {
    if (event.type !== 'lbutton-down' || event.button !== 0) {
      return null
    }
    const target = event.target
    if (target?.kind === 'resize-handle' && context.capabilities.canTransform) {
      const object = context.document.objects[target.objectId]
      return object ? new ResizeObjectMono(event, object, target.handle) : null
    }
    if (target?.kind === 'rotate-handle' && context.capabilities.canTransform) {
      const object = context.document.objects[target.objectId]
      return object ? new RotateObjectMono(event, object) : null
    }
    if (target?.kind === 'object') {
      const selected = event.modifiers.shift
        ? context.selection.ids.includes(target.objectId)
          ? context.selection.ids.filter((id) => id !== target.objectId)
          : [...context.selection.ids, target.objectId]
        : context.selection.ids.includes(target.objectId)
          ? context.selection.ids
          : [target.objectId]
      context.emit({ type: 'selection-changed', objectIds: selected })
      if (selected.length === 0) {
        return null
      }
      const objects = selected.flatMap((id) => {
        const object = context.document.objects[id]
        return object ? [object] : []
      })
      return objects.length > 0 ? new MoveObjectsMono(event, objects) : null
    }
    if (!target || target.kind === 'canvas') {
      context.emit({ type: 'selection-changed', objectIds: [] })
      return new MarqueeSelectMono(event)
    }
    return null
  }

  override onInput(event: CanvasInputEvent, context: LayerContext): EventResult {
    if (isMouse(event) && event.type === 'double-click' && event.target?.kind === 'object') {
      const object = context.document.objects[event.target.objectId]
      if (object?.kind === 'text') {
        context.emit({
          type: 'text-edit-requested',
          objectId: object.id,
          screenRect: {
            x: (object.x - context.camera.x) * context.camera.scale,
            y: (object.y - context.camera.y) * context.camera.scale,
            width: object.width * context.camera.scale,
            height: object.height * context.camera.scale,
          },
        })
        return HANDLED
      }
    }
    return super.onInput(event, context)
  }
}

export class IntentLayer implements EventLayer {
  constructor(readonly id: Exclude<LayerId, 'canvas' | 'select' | 'readonly-select'>) {}

  hit(_event: CanvasMouseEvent, _context: LayerContext): EventMono | null {
    return null
  }

  onInput(event: CanvasInputEvent, context: LayerContext): EventResult {
    if (!isMouse(event)) {
      return PASS
    }
    if (this.id === 'text' && event.type === 'double-click' && event.target?.kind === 'object') {
      const object = context.document.objects[event.target.objectId]
      if (object?.kind === 'text') {
        const screen = {
          x: (object.x - context.camera.x) * context.camera.scale,
          y: (object.y - context.camera.y) * context.camera.scale,
          width: object.width * context.camera.scale,
          height: object.height * context.camera.scale,
        }
        context.emit({ type: 'text-edit-requested', objectId: object.id, screenRect: screen })
        return HANDLED
      }
    }
    if (this.id === 'recognizing') {
      return HANDLED
    }
    return PASS
  }
}

export class ConnectLayer implements EventLayer {
  readonly id = 'connect' as const

  hit(event: CanvasMouseEvent, _context: LayerContext): EventMono | null {
    if (
      event.type === 'lbutton-down' &&
      event.target?.kind === 'port' &&
      event.target.port === 'output'
    ) {
      return new ConnectMono(event, event.target.objectId)
    }
    return null
  }

  onInput(_event: CanvasInputEvent, _context: LayerContext): EventResult {
    return PASS
  }
}

export class TextLayer implements EventLayer {
  readonly id = 'text' as const

  hit(event: CanvasMouseEvent, _context: LayerContext): EventMono | null {
    if (event.type === 'lbutton-down' && (!event.target || event.target.kind === 'canvas')) {
      return new TextCreateMono(event)
    }
    return null
  }

  onInput(event: CanvasInputEvent, context: LayerContext): EventResult {
    if (!isMouse(event)) {
      return PASS
    }
    if (event.type === 'double-click' && event.target?.kind === 'object') {
      const object = context.document.objects[event.target.objectId]
      if (object?.kind === 'text') {
        context.emit({
          type: 'text-edit-requested',
          objectId: object.id,
          screenRect: {
            x: (object.x - context.camera.x) * context.camera.scale,
            y: (object.y - context.camera.y) * context.camera.scale,
            width: object.width * context.camera.scale,
            height: object.height * context.camera.scale,
          },
        })
        return HANDLED
      }
    }
    return PASS
  }
}

export class MaskLayer implements EventLayer {
  readonly id = 'mask' as const

  hit(event: CanvasMouseEvent, _context: LayerContext): EventMono | null {
    if (event.type === 'lbutton-down' && event.target?.kind === 'object') {
      return new MaskPaintMono(event, event.target.objectId)
    }
    return null
  }

  onInput(_event: CanvasInputEvent, _context: LayerContext): EventResult {
    return HANDLED
  }
}

export class InpaintLayer implements EventLayer {
  readonly id = 'inpaint' as const

  hit(event: CanvasMouseEvent, _context: LayerContext): EventMono | null {
    if (event.type === 'lbutton-down' && event.target?.kind === 'object') {
      return new InpaintRegionMono(event, event.target.objectId)
    }
    return null
  }

  onInput(_event: CanvasInputEvent, _context: LayerContext): EventResult {
    return HANDLED
  }
}

export class LayerController {
  private readonly layers = new Map<LayerId, EventLayer>()
  private state: LayerState = {
    base: 'select',
    tool: null,
    temporary: [],
    modal: null,
    activeMono: null,
  }
  private activeMono: EventMono | null = null

  register(layer: EventLayer): void {
    this.layers.set(layer.id, layer)
  }

  getState(): LayerState {
    return { ...this.state, temporary: [...this.state.temporary] }
  }

  setReadonly(readonly: boolean): void {
    this.state = { ...this.state, base: readonly ? 'readonly-select' : 'select' }
  }

  activateTool(tool: CanvasTool): void {
    this.state = {
      ...this.state,
      tool: tool === 'select' ? null : tool === 'pan' ? 'canvas' : tool,
    }
  }

  pushTemporary(layer: LayerId): void {
    this.state = { ...this.state, temporary: [...this.state.temporary, layer] }
  }

  popTemporary(layer: LayerId): void {
    const index = this.state.temporary.lastIndexOf(layer)
    if (index < 0) {
      return
    }
    this.state = {
      ...this.state,
      temporary: this.state.temporary.filter((_, current) => current !== index),
    }
  }

  enterModal(layer: LayerId): void {
    this.state = { ...this.state, modal: layer }
  }

  exitModal(layer: LayerId): void {
    if (this.state.modal === layer) {
      this.state = { ...this.state, modal: null }
    }
  }

  effectiveLayerId(): LayerId {
    return this.state.modal ?? this.state.temporary.at(-1) ?? this.state.tool ?? this.state.base
  }

  private dispatchChain(): EventLayer[] {
    const ids = [
      this.state.modal,
      ...[...this.state.temporary].reverse(),
      this.state.tool,
      this.state.base,
      'canvas' as const,
    ].filter((value): value is LayerId => value !== null)
    return [...new Set(ids)].flatMap((id) => {
      const layer = this.layers.get(id)
      return layer ? [layer] : []
    })
  }

  dispatch(event: CanvasInputEvent, context: LayerContext): EventResult {
    if (this.activeMono) {
      const result = this.dispatchMono(this.activeMono, event, context)
      if (result.type === 'complete' || result.type === 'cancel') {
        if (result.type === 'cancel') {
          this.activeMono.cancel(result.reason, context)
        }
        this.activeMono = null
        this.state = { ...this.state, activeMono: null }
      }
      return result
    }

    for (const layer of this.dispatchChain()) {
      if (isMouse(event) && event.type === 'lbutton-down') {
        const mono = layer.hit(event, context)
        if (mono) {
          const entered = mono.onLButtonDown(event, context)
          if (entered.type !== 'cancel') {
            this.activeMono = mono
            this.state = { ...this.state, activeMono: mono.id }
            return { type: 'capture', mono }
          }
          return entered
        }
      }
      const result = layer.onInput(event, context)
      if (result.type !== 'pass') {
        return result
      }
    }
    return PASS
  }

  cancel(reason: InteractionCancelReason, context: LayerContext): EventMono | null {
    const mono = this.activeMono
    if (mono) {
      mono.cancel(reason, context)
    }
    this.activeMono = null
    this.state = { ...this.state, activeMono: null }
    return mono
  }

  private dispatchMono(
    mono: EventMono,
    event: CanvasInputEvent,
    context: LayerContext,
  ): EventResult {
    if (!isMouse(event)) {
      if (event.type === 'key-down' && event.key === 'Escape') {
        return { type: 'cancel', reason: 'escape' }
      }
      return HANDLED
    }
    switch (event.type) {
      case 'move':
        return mono.onLButtonMove(event, context)
      case 'lbutton-up':
        return mono.onLButtonUp(event, context)
      case 'rbutton-down':
        return mono.onRButtonDown?.(event, context) ?? { type: 'cancel', reason: 'escape' }
      case 'rbutton-up':
        return mono.onRButtonUp?.(event, context) ?? HANDLED
      case 'pointer-cancel':
      case 'leave':
        return { type: 'cancel', reason: 'pointer-cancel' }
      default:
        return HANDLED
    }
  }
}
