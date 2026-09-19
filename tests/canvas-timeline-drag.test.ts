import './dom.js'
import test from 'node:test'
import assert from 'node:assert/strict'
import { createTimelineCanvas } from '../apps/web/canvas/components/timeline-canvas.js'
import { createTimelineTrack } from '../apps/web/canvas/components/timeline-track.js'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'
import { emptyTimeline } from '../apps/web/canvas/state/timeline.js'
import type { CanvasEngine } from '../apps/web/canvas/engine/stage.js'

test('拖动未提交时连线跟随实时节点，支持多选、缩放和缺失节点回退，并清理监听', async () => {
  const objects = Object.fromEntries(
    ['a', 'b'].map((id) => [
      id,
      {
        id,
        kind: 'image' as const,
        x: 0,
        y: 0,
        width: 100,
        height: 80,
      },
    ]),
  )
  const store = createDocStore({
    id: 'doc',
    name: '拖动',
    objects,
    order: ['a', 'b'],
    timeline: {
      ...emptyTimeline(),
      canvas: { x: 500, y: 200 },
      clips: ['a', 'b'].map((id) => ({
        id,
        name: id,
        source: { nodeId: id, kind: 'image' as const, url: '/a.png', durationFrames: 150 },
        inFrame: 0,
        outFrame: 150,
      })),
    },
  })
  const positions = { a: { x: 0, y: 0 }, b: { x: 0, y: 0 } }
  let visible = true
  let listener: (() => void) | undefined
  let removed = ''
  const engine = {
    stage: {
      container: () => ({
        getBoundingClientRect: () => ({ left: 10, top: 20, right: 1010, bottom: 820 }),
      }),
      on(events: string, fn: () => void) {
        assert.match(events, /dragmove\.timelineCanvas/)
        listener = fn
      },
      off(events: string) {
        removed = events
      },
    },
    contentLayer: {
      getChildren: () =>
        visible
          ? (['a', 'b'] as const).map((id) => ({
              id: () => id,
              x: () => positions[id].x,
              y: () => positions[id].y,
              rotation: () => 0,
            }))
          : [],
    },
    getCam: () => ({ x: 0, y: 0, scale: 2 }),
    worldToScreen: (p: { x: number; y: number }) => ({ x: p.x * 2, y: p.y * 2 }),
    onSizeChanged: () => () => {},
  } as unknown as CanvasEngine
  const noop = () => {}
  const track = createTimelineTrack({ select: noop, seek: noop, move: noop, add: noop })
  const canvas = createTimelineCanvas({
    engine,
    store,
    track,
    play: noop,
    expand: noop,
    export: noop,
    add: noop,
    move: noop,
  })
  const paths = () =>
    Array.from(document.querySelectorAll('.tl-canvas-connections path')).map((path) =>
      path.getAttribute('d'),
    )
  try {
    canvas.refresh()
    assert.match(paths()[0]!, /^M 210 100 /)
    positions.a = { x: 70, y: 35 }
    positions.b = { x: 90, y: 50 }
    listener!()
    listener!()
    await Promise.resolve()
    assert.match(paths()[0]!, /^M 350 170 /)
    assert.match(paths()[1]!, /^M 390 200 /)
    assert.equal(store.doc.objects.a.x, 0)
    assert.equal(store.canUndo(), false)
    visible = false
    listener!()
    await Promise.resolve()
    assert.match(paths()[0]!, /^M 210 100 /)
    listener!()
    canvas.dispose()
    await Promise.resolve()
    assert.equal(removed, '.timelineCanvas')
    assert.equal(document.querySelector('.tl-canvas-overlay'), null)
  } finally {
    canvas.dispose()
    track.dispose()
  }
})
