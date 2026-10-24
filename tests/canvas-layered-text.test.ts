import assert from 'node:assert/strict'
import test from 'node:test'

import { containFrame, coverCrop } from '../apps/web/canvas/renderer-konva/image-fit.js'
import { applyCommand, type CanvasObj } from '../apps/web/canvas/state/commands.js'
import { textStyleUpdate } from '../apps/web/canvas/state/text-style.js'

const PRESETS = [
  { id: 'feed_square', width: 1080, height: 1080 },
  { id: 'story_portrait', width: 1080, height: 1920 },
  { id: 'banner_landscape', width: 1920, height: 1080 },
]

test('contain keeps the whole subject inside every preset frame', () => {
  const source = { width: 540, height: 540 }
  for (const preset of PRESETS) {
    const frame = containFrame(source.width, source.height, preset.width, preset.height)
    assert.equal(frame.width, frame.height)
    assert.ok(frame.x >= 0 && frame.y >= 0)
    assert.ok(frame.x + frame.width <= preset.width + 0.001)
    assert.ok(frame.y + frame.height <= preset.height + 0.001)
    const cover = coverCrop(source.width, source.height, preset.width, preset.height)
    if (preset.width !== preset.height) {
      assert.notEqual(cover.width / cover.height, frame.width / frame.height)
    }
  }
})

test('ordinary images still cover-crop and layered text reloads', () => {
  const wide = coverCrop(400, 100, 100, 100)
  assert.ok(wide.width < 400)
  const object: CanvasObj = {
    id: 't1',
    kind: 'text',
    x: 8,
    y: 6,
    width: 200,
    height: 40,
    text: '月圆人团圆',
    fontSize: 32,
    fontFamily: 'songti-sc',
    fill: '#112233',
    textAlign: 'center',
    region: 'title',
    layerRole: 'text',
  }
  const update = textStyleUpdate(object, { fill: '#abcdef', fontSize: 40 })
  assert.ok(update)
  const doc = { objects: { t1: object }, order: ['t1'] }
  applyCommand(doc, { type: 'updateObject', id: 't1', patch: update.patch, prevPatch: update.prev })
  const saved = JSON.parse(JSON.stringify(doc)) as typeof doc
  assert.equal(saved.objects.t1.text, '月圆人团圆')
  assert.equal(saved.objects.t1.fill, '#abcdef')
  assert.equal(saved.objects.t1.fontFamily, 'songti-sc')
  assert.equal(saved.objects.t1.textAlign, 'center')
  assert.equal(saved.objects.t1.region, 'title')
  assert.equal(textStyleUpdate(object, { fill: 'red' }), null)
})
