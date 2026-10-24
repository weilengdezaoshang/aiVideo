import Konva from 'konva'
import {
  useEffect,
  useRef,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  type RefObject,
} from 'react'
import {
  Circle,
  Group,
  Image as KonvaImage,
  Label,
  Layer,
  Line,
  Rect,
  Stage,
  Tag,
  Text,
} from 'react-konva'
import type { KonvaEventObject } from 'konva/lib/Node'

import { useCanvasView, useCanvasViewSnapshot } from '../react/canvas-view-context.js'
import type { CanvasObj } from '../state/commands.js'
import { connectionPoints, documentConnections, withObjectPreviews } from '../state/connections.js'
import type {
  CanvasHitTarget,
  CanvasInputEvent,
  CanvasModifiers,
  CanvasMouseEvent,
  ResizeHandle,
} from '../core/types.js'
import { useImageElement, useVideoElement } from './use-media.js'
import { containFrame, coverCrop } from './image-fit.js'
import { TextPropertyBar } from './text-properties.js'
import { layeredLines } from './layered-text.js'
import { cmdUpdateObject } from '../state/commands.js'
import { exposeRendererDiagnostics } from './perf-diagnostics.js'
import { preventBrowserGestureZoom } from './browser-gestures.js'
import type { Theme } from '../theme.js'

const SELECTION_COLOR = '#3478f6'

export type CanvasEditorProps = {
  className?: string
  style?: CSSProperties
  theme?: Theme
  showMask?: boolean
}

export function CanvasEditor({
  className,
  style,
  theme = 'light',
  showMask = true,
}: CanvasEditorProps) {
  useEffect(exposeRendererDiagnostics, [])
  const view = useCanvasView()
  const snapshot = useCanvasViewSnapshot()
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const document = containerRef.current?.ownerDocument
    if (document) {
      return preventBrowserGestureZoom(document)
    }
  }, [])

  useEffect(() => {
    const element = containerRef.current
    if (!element) {
      return
    }
    const update = () => view.setViewportSize(element.clientWidth, element.clientHeight)
    update()
    const observer = new ResizeObserver(update)
    observer.observe(element)
    return () => observer.disconnect()
  }, [view])

  const dispatchMouse = (
    type: CanvasMouseEvent['type'],
    event: KonvaEventObject<MouseEvent | WheelEvent>,
  ) => {
    const stage = event.target.getStage()
    const point = stage?.getPointerPosition()
    if (!point) {
      return
    }
    if (type === 'lbutton-down' || type === 'rbutton-down') {
      containerRef.current?.focus({ preventScroll: true })
    }
    const native = event.evt
    const target = resolveHitTarget(event.target)
    const input: CanvasMouseEvent = {
      type,
      pointerId:
        'pointerId' in native && typeof native.pointerId === 'number' ? native.pointerId : 1,
      button: 'button' in native ? native.button : 0,
      screenPoint: point,
      worldPoint: view.screenToWorld(point),
      modifiers: modifiersFrom(native),
      target,
      ...('deltaX' in native
        ? {
            wheelDelta: {
              x:
                native.deltaX *
                (native.deltaMode === 1
                  ? 16
                  : native.deltaMode === 2
                    ? snapshot.viewport.width
                    : 1),
              y:
                native.deltaY *
                (native.deltaMode === 1
                  ? 16
                  : native.deltaMode === 2
                    ? snapshot.viewport.height
                    : 1),
            },
            wheelTime: native.timeStamp,
          }
        : {}),
    }
    view.dispatchInput(input)
  }

  const selectedId = snapshot.selection.length === 1 ? snapshot.selection[0] : undefined
  const selectedObject = selectedId ? view.getDocument().objects[selectedId] : undefined
  return (
    <div
      ref={containerRef}
      data-canvas-keyboard-target
      className={className}
      tabIndex={0}
      style={{
        width: '100%',
        height: '100%',
        overflow: 'hidden',
        outline: 'none',
        display: 'flex',
        flexDirection: 'column',
        cursor:
          snapshot.requestedTool === 'mask'
            ? maskBrushCursor(snapshot.maskBrush.size, snapshot.maskBrush.mode)
            : undefined,
        ...style,
      }}
      onKeyDown={(event) => {
        if ((event.metaKey || event.ctrlKey) && ['a', 'd', 'z'].includes(event.key.toLowerCase())) {
          event.preventDefault()
        }
        view.dispatchInput(keyboardInput('key-down', event))
      }}
      onKeyUp={(event) => view.dispatchInput(keyboardInput('key-up', event))}
    >
      {selectedObject?.kind === 'text' && (
        <TextPropertyBar
          object={selectedObject}
          onPatch={(patch, prev) => view.execute(cmdUpdateObject(selectedObject.id, patch, prev))}
        />
      )}
      <Stage
        width={snapshot.viewport.width}
        height={snapshot.viewport.height}
        onMouseDown={(event) => dispatchMouse(buttonType(event.evt.button, true), event)}
        onMouseUp={(event) => dispatchMouse(buttonType(event.evt.button, false), event)}
        onMouseMove={(event) => dispatchMouse('move', event)}
        onMouseEnter={(event) => dispatchMouse('enter', event)}
        onMouseLeave={(event) => dispatchMouse('leave', event)}
        onDblClick={(event) => dispatchMouse('double-click', event)}
        onContextMenu={(event) => {
          event.evt.preventDefault()
          dispatchMouse('context-menu', event)
        }}
        onWheel={(event) => {
          event.evt.preventDefault()
          dispatchMouse('wheel', event)
        }}
      >
        <Layer listening={false}>
          <Rect
            width={snapshot.viewport.width}
            height={snapshot.viewport.height}
            fill="transparent"
          />
        </Layer>
        <CameraLayer listening={false}>
          <ConnectionScene theme={theme} />
        </CameraLayer>
        <CameraLayer>
          <ContentScene theme={theme} />
        </CameraLayer>
        <CameraLayer>
          <SelectionScene />
          <EditingScene showMask={showMask} />
        </CameraLayer>
      </Stage>
    </div>
  )
}

/** 相机变换层：分层后各层共用同一取景，交互变化只重绘所在层。 */
function CameraLayer({ children, listening }: { children: ReactNode; listening?: boolean }) {
  const snapshot = useCanvasViewSnapshot()
  return (
    <Layer
      listening={listening}
      x={-snapshot.camera.x * snapshot.camera.scale}
      y={-snapshot.camera.y * snapshot.camera.scale}
      scaleX={snapshot.camera.scale}
      scaleY={snapshot.camera.scale}
    >
      {children}
    </Layer>
  )
}

function ContentScene({ theme }: { theme: Theme }) {
  const view = useCanvasView()
  const snapshot = useCanvasViewSnapshot()
  const doc = view.getDocument()
  const objects = withObjectPreviews(doc.objects, snapshot.objectPreviews)
  return (
    <Group>
      {doc.order.map((id) => {
        const object = objects[id]
        if (!object) {
          return null
        }
        return (
          <ObjectNode
            key={id}
            object={object}
            theme={theme}
            selected={snapshot.selection.includes(id)}
            hovered={snapshot.hoveredId === id}
          />
        )
      })}
    </Group>
  )
}

function ConnectionScene({ theme }: { theme: Theme }) {
  const view = useCanvasView()
  const snapshot = useCanvasViewSnapshot()
  const objects = withObjectPreviews(view.getDocument().objects, snapshot.objectPreviews)
  return (
    <Group listening={false}>
      {documentConnections(objects).map((edge) => {
        const source = objects[edge.sourceId]
        const target = objects[edge.targetId]
        if (!source || !target) {
          return null
        }
        const active =
          snapshot.selection.includes(source.id) || snapshot.selection.includes(target.id)
        return (
          <Line
            key={`${edge.sourceId}:${edge.targetId}:${edge.kind}`}
            points={connectionPoints(source, target)}
            bezier
            stroke={active ? SELECTION_COLOR : theme === 'dark' ? '#c9ccd4' : '#9aa0a8'}
            strokeWidth={2 / snapshot.camera.scale}
            opacity={active ? 1 : 0.72}
          />
        )
      })}
    </Group>
  )
}

function ObjectNode({
  object,
  selected,
  hovered,
  theme,
}: {
  theme: Theme
  object: CanvasObj
  selected: boolean
  hovered: boolean
}) {
  const src = mediaUrl(object)
  const image = useImageElement(object.kind === 'image' ? src : null)
  const video = useVideoElement(object.kind === 'video' ? src : null)
  const videoNode = useRef<Konva.Image>(null)

  useEffect(() => {
    if (!video || !videoNode.current) {
      return
    }
    const animation = new Konva.Animation(() => {}, videoNode.current.getLayer())
    animation.start()
    return () => {
      animation.stop()
    }
  }, [video])

  const stroke = selected ? SELECTION_COLOR : hovered ? '#79a8ff' : undefined
  const common = {
    width: object.width,
    height: object.height,
    cornerRadius: object.kind === 'board' ? 4 : 10,
    stroke,
    strokeWidth: selected || hovered ? 2 : 0,
  }
  return (
    <Group
      x={object.x}
      y={object.y}
      rotation={object.rotation ?? 0}
      objectId={object.id}
      hitKind="object"
      name="canvas-object"
    >
      {object.kind === 'board' && (
        <Rect
          {...common}
          fill={object.backgroundColor || '#ffffff'}
          shadowBlur={8}
          shadowOpacity={0.08}
        />
      )}
      {object.kind === 'text' && (
        <>
          {object.textCard && <Rect {...common} fill={object.backgroundColor || '#ffffff'} />}
          {layeredLines(object) ? (
            layeredLines(object)!.map((line, index) => (
              <Text
                key={`${line.text}-${index}`}
                x={line.x}
                y={line.y}
                width={Math.max(line.width, 1)}
                height={line.height}
                text={line.text}
                fontSize={object.fontSize || 16}
                fontFamily={object.fontFamily}
                align="left"
                fill={object.fill || '#202124'}
                padding={0}
                verticalAlign="top"
              />
            ))
          ) : (
            <Text
              width={object.width}
              height={object.height}
              text={object.text || '双击输入文字'}
              fontSize={object.fontSize || 16}
              fontFamily={object.fontFamily}
              align={object.textAlign || 'left'}
              fill={object.fill || '#202124'}
              padding={12}
              verticalAlign="middle"
            />
          )}
        </>
      )}
      {(object.kind === 'image' || object.kind === 'video') && (image || video) && (
        <FittedImage
          nodeRef={videoNode}
          object={object}
          media={(video || image) as CanvasImageSource}
          common={common}
        />
      )}
      {(object.kind === 'image' || object.kind === 'video') && !image && !video && (
        <Rect {...common} fill={theme === 'dark' ? '#25262c' : '#e8eaed'} />
      )}
      {(object.kind === 'placeholder' || object.nodeRun) && (
        <>
          <Rect {...common} fill={theme === 'dark' ? '#25262c' : '#eef3ff'} dash={[8, 6]} />
          <Text
            width={object.width}
            height={object.height}
            text={
              object.gen?.message ||
              object.nodeRun?.message ||
              (object.placeholderType ? object.name || '空占位符' : '生成中…')
            }
            align="center"
            verticalAlign="middle"
            fill={theme === 'dark' ? '#9aa0a8' : '#4771c5'}
          />
        </>
      )}
      {object.kind === 'error' && (
        <>
          <Rect {...common} fill="#fff0f0" stroke="#d93025" strokeWidth={1} />
          <Text
            width={object.width}
            height={object.height}
            padding={16}
            text={object.errorDetail || '生成失败'}
            align="center"
            verticalAlign="middle"
            fill="#b3261e"
          />
        </>
      )}
      {object.name && object.kind === 'board' && (
        <Label y={-28} listening={false}>
          <Tag fill="#202124" cornerRadius={4} />
          <Text text={object.name} fill="white" fontSize={12} padding={6} />
        </Label>
      )}
      {object.badge && (
        <Label x={8} y={8} listening={false}>
          <Tag
            fill={object.badge === 'review_soft_fail' ? '#9a6700' : '#202124'}
            cornerRadius={4}
          />
          <Text
            text={
              object.badge === 'preview'
                ? '样片预览'
                : object.badge === 'final'
                  ? '正片'
                  : '质量待确认'
            }
            fill="white"
            fontSize={12}
            padding={6}
          />
        </Label>
      )}
      <ConnectionPorts object={object} />
    </Group>
  )
}

function ConnectionPorts({ object }: { object: CanvasObj }) {
  const snapshot = useCanvasViewSnapshot()
  if (snapshot.effectiveLayer !== 'connect') {
    return null
  }
  const radius = 7 / snapshot.camera.scale
  return (
    <>
      <Circle
        x={0}
        y={object.height / 2}
        radius={radius}
        fill="#ffffff"
        stroke="#5b7fd7"
        strokeWidth={2 / snapshot.camera.scale}
        objectId={object.id}
        hitKind="port"
        port="input"
      />
      <Circle
        x={object.width}
        y={object.height / 2}
        radius={radius}
        fill="#5b7fd7"
        stroke="#ffffff"
        strokeWidth={2 / snapshot.camera.scale}
        objectId={object.id}
        hitKind="port"
        port="output"
      />
    </>
  )
}

function SelectionScene() {
  const view = useCanvasView()
  const snapshot = useCanvasViewSnapshot()
  if (
    ['mask', 'recognizing'].includes(snapshot.requestedTool) ||
    !snapshot.capabilities.canTransform ||
    snapshot.selection.length !== 1
  ) {
    return null
  }
  const id = snapshot.selection[0]
  const object = withObjectPreviews(view.getDocument().objects, snapshot.objectPreviews)[id]
  if (!object) {
    return null
  }
  const size = 9 / snapshot.camera.scale
  const half = size / 2
  const handles: { handle: ResizeHandle; x: number; y: number }[] = [
    { handle: 'nw', x: 0, y: 0 },
    { handle: 'n', x: object.width / 2, y: 0 },
    { handle: 'ne', x: object.width, y: 0 },
    { handle: 'e', x: object.width, y: object.height / 2 },
    { handle: 'se', x: object.width, y: object.height },
    { handle: 's', x: object.width / 2, y: object.height },
    { handle: 'sw', x: 0, y: object.height },
    { handle: 'w', x: 0, y: object.height / 2 },
  ]
  return (
    <Group x={object.x} y={object.y} rotation={object.rotation ?? 0}>
      <Rect
        width={object.width}
        height={object.height}
        stroke="#3478f6"
        strokeWidth={1 / snapshot.camera.scale}
        listening={false}
      />
      {handles.map(({ handle, x, y }) => (
        <Rect
          key={handle}
          x={x - half}
          y={y - half}
          width={size}
          height={size}
          fill="white"
          stroke="#3478f6"
          strokeWidth={1 / snapshot.camera.scale}
          objectId={id}
          hitKind="resize-handle"
          handle={handle}
        />
      ))}
      <Circle
        x={object.width / 2}
        y={-28 / snapshot.camera.scale}
        radius={half}
        fill="white"
        stroke="#3478f6"
        strokeWidth={1 / snapshot.camera.scale}
        objectId={id}
        hitKind="rotate-handle"
      />
    </Group>
  )
}

function EditingScene({ showMask }: { showMask: boolean }) {
  const snapshot = useCanvasViewSnapshot()
  if (
    !snapshot.marquee &&
    !snapshot.connectionPreview &&
    !snapshot.maskStroke &&
    snapshot.maskStrokes.length === 0 &&
    !snapshot.inpaintRegion
  ) {
    return null
  }
  return (
    <Group listening={false}>
      {snapshot.marquee && (
        <Rect
          {...snapshot.marquee}
          fill="rgba(52,120,246,.10)"
          stroke="#3478f6"
          strokeWidth={1 / snapshot.camera.scale}
        />
      )}
      {snapshot.connectionPreview && (
        <Line
          points={[
            snapshot.connectionPreview.start.x,
            snapshot.connectionPreview.start.y,
            snapshot.connectionPreview.end.x,
            snapshot.connectionPreview.end.y,
          ]}
          stroke="#3478f6"
          strokeWidth={2 / snapshot.camera.scale}
          dash={[8 / snapshot.camera.scale, 6 / snapshot.camera.scale]}
        />
      )}
      {showMask && snapshot.maskStroke && snapshot.maskStroke.length > 1 && (
        <Line
          points={snapshot.maskStroke.flatMap((point) => [point.x, point.y])}
          stroke="rgba(255,72,102,.72)"
          strokeWidth={22 / snapshot.camera.scale}
          lineCap="round"
          lineJoin="round"
        />
      )}
      {showMask &&
        snapshot.maskStrokes.map((stroke, index) => (
          <Line
            key={`mask-stroke-${index}`}
            points={stroke.flatMap((point) => [point.x, point.y])}
            stroke="rgba(255,72,102,.72)"
            strokeWidth={22 / snapshot.camera.scale}
            lineCap="round"
            lineJoin="round"
          />
        ))}
      {snapshot.inpaintRegion && (
        <Rect
          {...snapshot.inpaintRegion}
          fill="rgba(151,92,255,.16)"
          stroke="#8754d8"
          strokeWidth={2 / snapshot.camera.scale}
          dash={[8 / snapshot.camera.scale, 5 / snapshot.camera.scale]}
        />
      )}
    </Group>
  )
}

function resolveHitTarget(node: Konva.Node): CanvasHitTarget {
  let current: Konva.Node | null = node
  while (current) {
    const hitKind = current.getAttr('hitKind') as CanvasHitTarget['kind'] | undefined
    const objectId = current.getAttr('objectId') as string | undefined
    if (hitKind === 'object' && objectId) {
      return { kind: 'object', objectId }
    }
    if (hitKind === 'resize-handle' && objectId) {
      return { kind: 'resize-handle', objectId, handle: current.getAttr('handle') as ResizeHandle }
    }
    if (hitKind === 'rotate-handle' && objectId) {
      return { kind: 'rotate-handle', objectId }
    }
    if (hitKind === 'port' && objectId) {
      const port = current.getAttr('port') as 'input' | 'output'
      return { kind: 'port', objectId, port }
    }
    current = current.getParent()
  }
  return { kind: 'canvas' }
}

function buttonType(button: number, down: boolean): CanvasMouseEvent['type'] {
  if (button === 2) {
    return down ? 'rbutton-down' : 'rbutton-up'
  }
  return down ? 'lbutton-down' : 'lbutton-up'
}

function modifiersFrom(event: MouseEvent | WheelEvent | KeyboardEvent): CanvasModifiers {
  return {
    shift: event.shiftKey,
    alt: event.altKey,
    ctrl: event.ctrlKey,
    meta: event.metaKey,
    space: 'code' in event && event.code === 'Space',
  }
}

function keyboardInput(type: 'key-down' | 'key-up', event: ReactKeyboardEvent): CanvasInputEvent {
  return { type, key: event.key, code: event.code, modifiers: modifiersFrom(event.nativeEvent) }
}

function mediaUrl(object: CanvasObj): string | null {
  if (object.src) {
    return object.src
  }
  if (!object.assetId) {
    return null
  }
  return `/assets/${object.assetId}/original.${object.ext || (object.kind === 'video' ? 'mp4' : 'png')}`
}

function usesContain(object: CanvasObj): boolean {
  return (
    object.fit === 'contain' || object.layerRole === 'subject' || object.layerRole === 'background'
  )
}

function FittedImage({
  object,
  media,
  common,
  nodeRef,
}: {
  object: CanvasObj
  media: CanvasImageSource
  common: {
    width: number
    height: number
    cornerRadius: number
    stroke?: string
    strokeWidth: number
  }
  nodeRef: RefObject<Konva.Image | null>
}) {
  const sourceWidth = mediaWidth(media)
  const sourceHeight = mediaHeight(media)
  const contain = usesContain(object) && sourceWidth > 0 && sourceHeight > 0
  const frame = contain
    ? containFrame(sourceWidth, sourceHeight, object.width, object.height)
    : null
  return (
    <KonvaImage
      ref={nodeRef}
      {...common}
      x={frame?.x}
      y={frame?.y}
      width={frame?.width ?? common.width}
      height={frame?.height ?? common.height}
      image={media}
      crop={
        contain || !sourceWidth || !sourceHeight
          ? undefined
          : coverCrop(sourceWidth, sourceHeight, object.width, object.height)
      }
    />
  )
}

function mediaWidth(media: CanvasImageSource): number {
  if ('videoWidth' in media) {
    return media.videoWidth
  }
  if ('naturalWidth' in media) {
    return media.naturalWidth
  }
  if ('displayWidth' in media) {
    return media.displayWidth
  }
  return 'width' in media && typeof media.width === 'number' ? media.width : 0
}

function mediaHeight(media: CanvasImageSource): number {
  if ('videoHeight' in media) {
    return media.videoHeight
  }
  if ('naturalHeight' in media) {
    return media.naturalHeight
  }
  if ('displayHeight' in media) {
    return media.displayHeight
  }
  return 'height' in media && typeof media.height === 'number' ? media.height : 0
}

function maskBrushCursor(size: number, mode: 'paint' | 'erase'): string {
  const diameter = Math.min(120, Math.max(4, size))
  const side = diameter + 4
  const center = side / 2
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${side}" height="${side}"><circle cx="${center}" cy="${center}" r="${diameter / 2}" fill="none" stroke="black" stroke-width="3"/><circle cx="${center}" cy="${center}" r="${diameter / 2}" fill="none" stroke="white" stroke-width="1"/><path d="M${center - 3} ${center}h6${mode === 'paint' ? `M${center} ${center - 3}v6` : ''}" stroke="white"/></svg>`
  return `url("data:image/svg+xml,${encodeURIComponent(svg)}") ${center} ${center}, crosshair`
}
