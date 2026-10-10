import './dom.js'
import test from 'node:test'
import assert from 'node:assert/strict'
import { cleanup, fireEvent, render, screen, act } from '@testing-library/react'
import { TimelineStudio } from '../apps/web/canvas/timeline-studio.js'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'
import { emptyTimeline } from '../apps/web/canvas/state/timeline.js'

test('清空最后一个片段会停止播放；面板重新打开不会恢复旧播放', () => {
  const store = createDocStore({
    id: 'preview',
    name: 'preview',
    objects: {},
    order: [],
    timeline: {
      ...emptyTimeline(),
      clips: [
        {
          id: 'c',
          name: '图像',
          source: { nodeId: 'n', kind: 'image', url: '/image.png', durationFrames: 90 },
          inFrame: 0,
          outFrame: 90,
        },
      ],
    },
  })
  const ensureSaved = async () => {}
  const element = (active: boolean) => (
    <TimelineStudio
      store={store}
      media={[]}
      active={active}
      ensureSaved={ensureSaved}
      exportKey="test.preview"
    />
  )
  const view = render(element(true))
  try {
    fireEvent.click(screen.getByRole('button', { name: '播放' }))
    assert.ok(screen.getByRole('button', { name: '暂停' }))
    act(() =>
      store.apply({ type: 'setTimeline', before: store.doc.timeline, after: emptyTimeline() }),
    )
    view.rerender(element(true))
    assert.equal((screen.getByRole('button', { name: '播放' }) as HTMLButtonElement).disabled, true)
    view.rerender(element(false))
    view.rerender(element(true))
    assert.ok(screen.getByRole('button', { name: '播放' }))
  } finally {
    cleanup()
  }
})
