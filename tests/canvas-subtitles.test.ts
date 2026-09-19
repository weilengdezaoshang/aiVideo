import test from 'node:test'
import assert from 'node:assert/strict'
import { timelineSrt } from '../apps/web/canvas/state/subtitles.js'
import { emptyTimeline, splitClip } from '../apps/web/canvas/state/timeline.js'

test('字幕按剪辑后的帧时长排列，空字幕保留间隔，分割保持同步', () => {
  const source = { nodeId: 'n', kind: 'image' as const, url: '/image.png', durationFrames: 300 }
  const timeline = {
    ...emptyTimeline(),
    clips: [
      { id: 'a', name: 'a', source, inFrame: 0, outFrame: 30 },
      { id: 'b', name: 'b', source, inFrame: 60, outFrame: 120, subtitle: '你好\n\n世界' },
    ],
  }
  assert.equal(timelineSrt(timeline), '1\n00:00:01,000 --> 00:00:03,000\n你好\n世界\n')
  const split = splitClip(timeline, 60, 'c')
  assert.match(timelineSrt(split), /00:00:02,000 --> 00:00:03,000/)
  assert.equal(timelineSrt(emptyTimeline()), '')
})
