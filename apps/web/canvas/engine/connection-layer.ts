import type { CanvasEngine } from './stage.js'
import type { DocStore } from '../state/doc-store.js'
import { connectionPoints, documentConnections } from '../state/connections.js'
import { createConnectionInspector } from '../flows/connection-inspector.js'
import { connectReference } from '../flows/connect-reference.js'
import { transformPoint } from './transform-geometry.js'
import type { Geometry } from './transform-geometry.js'

/** 放在对象下方；点击查看关系，断开引用复用撤销，不产生生成请求。 */
export function createConnectionLayer(
  engine: CanvasEngine,
  store: DocStore,
  notify: (message: string) => void,
) {
  const layer = new Konva.Layer({ listening: true })
  engine.stage.add(layer)
  layer.moveToBottom()
  engine.registerCamLayer(layer)
  const inspector = createConnectionInspector(store)
  const operation = new AbortController()
  let dragging = false
  let disposed = false
  let queued = false
  const geometry = (id: string): Geometry => {
    const obj = store.doc.objects[id]
    // 多选拖动在释放前只更新 Konva；此处读取实时位置。
    const node = engine.contentLayer
      .getChildren()
      .find((child: { id(): string }) => child.id() === id)
    return node ? { ...obj, x: node.x(), y: node.y(), rotation: node.rotation() } : obj
  }
  function sync() {
    if (disposed || dragging) {
      return
    }
    layer.destroyChildren()
    const dark = document.documentElement.dataset.theme === 'dark'
    for (const edge of documentConnections(store.doc.objects)) {
      const arrow = new Konva.Arrow({
        points: connectionPoints(geometry(edge.sourceId), geometry(edge.targetId)),
        bezier: true,
        stroke: dark ? '#939bb5' : '#727c99',
        fill: dark ? '#939bb5' : '#727c99',
        strokeWidth: 1.5,
        strokeScaleEnabled: false,
        pointerLength: 8,
        pointerWidth: 7,
        dash: edge.kind === 'reference' ? [6, 4] : [],
        listening: true,
        hitStrokeWidth: 16,
      })
      arrow.on('click tap', (event: { cancelBubble: boolean }) => {
        event.cancelBubble = true
        inspector.inspect(edge)
      })
      layer.add(arrow)
    }
    for (const obj of Object.values(store.doc.objects)) {
      const output = obj.kind === 'image' && Boolean(obj.assetId || obj.src)
      const input =
        ['image', 'video'].includes(obj.kind) &&
        obj.nodeDraft &&
        !obj.nodeRun &&
        !obj.assetId &&
        !obj.src
      if (!output && !input) {
        continue
      }
      const geo = geometry(obj.id)
      const at = transformPoint(geo, output ? geo.width + 14 : -14, geo.height / 2)
      const port = new Konva.Circle({
        x: at.x,
        y: at.y,
        radius: 7,
        fill: output ? '#6578cf' : '#ffffff',
        stroke: '#6578cf',
        strokeWidth: 2,
        draggable: output,
        hitStrokeWidth: 12,
      })
      port.on('mousedown touchstart click tap', (event: { cancelBubble: boolean }) => {
        event.cancelBubble = true
      })
      if (output) {
        const preview = new Konva.Line({
          points: [],
          stroke: '#6578cf',
          strokeWidth: 2,
          strokeScaleEnabled: false,
          dash: [6, 4],
          listening: false,
          bezier: true,
        })
        layer.add(preview)
        port.on('dragstart', (event: { cancelBubble: boolean }) => {
          event.cancelBubble = true
          dragging = true
          preview.points([at.x, at.y, at.x, at.y, port.x(), port.y(), port.x(), port.y()])
        })
        port.on('dragmove', (event: { cancelBubble: boolean }) => {
          event.cancelBubble = true
          const reach = Math.max(48, Math.abs(port.x() - at.x) / 2)
          preview.points([
            at.x,
            at.y,
            at.x + reach,
            at.y,
            port.x() - reach,
            port.y(),
            port.x(),
            port.y(),
          ])
          layer.batchDraw()
        })
        port.on('dragend', (event: { cancelBubble: boolean }) => {
          event.cancelBubble = true
          dragging = false
          const scale = engine.getCam().scale
          const target = Object.values(store.doc.objects).find((candidate) => {
            if (
              candidate.id === obj.id ||
              !candidate.nodeDraft ||
              candidate.nodeRun ||
              candidate.src ||
              candidate.assetId
            ) {
              return false
            }
            const shape = geometry(candidate.id)
            const point = transformPoint(shape, -14, shape.height / 2)
            return Math.hypot(point.x - port.x(), point.y - port.y()) * scale < 24
          })
          sync()
          if (target) {
            void connectReference(store, obj.id, target.id, operation.signal)
              .then(() => notify('已连接参考图'))
              .catch((error: unknown) => {
                if (!operation.signal.aborted) {
                  notify(error instanceof Error ? error.message : '连接失败')
                }
              })
          } else {
            notify('拖到空生成节点左侧的圆形端口以添加参考')
          }
        })
      }
      layer.add(port)
    }
    layer.batchDraw()
  }
  function schedule() {
    if (!queued && !disposed) {
      queued = true
      queueMicrotask(() => {
        queued = false
        sync()
      })
    }
  }
  const unsubscribe = store.subscribe(schedule)
  const updateInteraction = () => {
    layer.listening(engine.interaction.getState().effective === 'select')
    layer.drawHit()
  }
  const unsubscribeInteraction = engine.interaction.subscribe(updateInteraction)
  updateInteraction()
  engine.stage.on('dragmove.connections dragend.connections transform.connections', schedule)
  document.addEventListener('gencanvas:themechange', schedule)
  sync()
  return {
    sync,
    dispose() {
      disposed = true
      operation.abort()
      unsubscribe()
      unsubscribeInteraction()
      engine.stage.off('.connections')
      document.removeEventListener('gencanvas:themechange', schedule)
      engine.unregisterCamLayer(layer)
      layer.destroy()
      inspector.dispose()
    },
  }
}
