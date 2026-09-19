import './dom.js'
import test from 'node:test'
import assert from 'node:assert/strict'
import { fireEvent } from '@testing-library/dom'
import { createTimelineTrack } from '../apps/web/canvas/components/timeline-track.js'
import { createTimelineAssets } from '../apps/web/canvas/components/timeline-assets.js'
import { emptyTimeline, type TimelineClip } from '../apps/web/canvas/state/timeline.js'

test('共享轨道按真实帧长定位，图片缩略图、选择和播放头使用相同比例', () => {
  let picked = ''
  let pickedFrame = -1
  let adds = 0
  const track = createTimelineTrack({
    select(id, frame) {
      picked = id
      pickedFrame = frame
    },
    seek() {},
    move() {},
    add() {
      adds++
    },
  })
  const clip: TimelineClip = {
    id: 'a',
    name: '<图片>',
    source: {
      nodeId: 'image',
      kind: 'image',
      url: '/image.png',
      durationFrames: 300,
    },
    inFrame: 0,
    outFrame: 150,
  }
  track.render({ ...emptyTimeline(), clips: [clip, { ...clip, id: 'b', outFrame: 30 }] }, 'a', 20)
  document.body.append(track.element)
  try {
    const clips = track.element.querySelectorAll<HTMLButtonElement>('.timeline-clip')
    assert.equal(clips[0].style.width, '100px')
    assert.equal(clips[1].style.width, '20px')
    assert.equal(clips[0].querySelector('img')?.getAttribute('src'), '/image.png')
    fireEvent.click(clips[1])
    assert.equal(picked, 'b')
    assert.equal(pickedFrame, 150)
    track.setFrame(165)
    assert.equal(track.element.querySelector<HTMLElement>('.tl-playhead')?.style.left, '110px')
    fireEvent.click(track.element.querySelector('.tl-add-clip')!)
    assert.equal(adds, 1)
  } finally {
    track.dispose()
  }
})

test('素材库仅允许完成素材加入，已添加状态从时间线引用计算', async () => {
  const ids: string[] = []
  const assets = createTimelineAssets({
    getObjects: () => [
      {
        id: 'ready',
        name: '图片',
        kind: 'image',
        src: '/image.png',
        x: 0,
        y: 0,
        width: 10,
        height: 10,
      },
      { id: 'pending', kind: 'video', x: 0, y: 0, width: 10, height: 10 },
    ],
    getAdded: () => new Set(['ready']),
    async add(next) {
      ids.push(...next)
    },
    async importFiles() {},
  })
  assets.render()
  assert.equal(assets.element.querySelectorAll('.tl-asset-card').length, 1)
  assert.equal(assets.element.querySelector('small')?.textContent, '已添加')
  fireEvent.click(assets.element.querySelector('.tl-asset-card')!)
  await Promise.resolve()
  assert.deepEqual(ids, ['ready'])
})
