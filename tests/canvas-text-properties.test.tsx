import './dom.js'
import React, { useState } from 'react'
import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { cleanup, fireEvent, render } from '@testing-library/react'

import { LayeredTextReadout } from '../apps/web/canvas/renderer-konva/layered-text.js'
import { TextPropertyBar } from '../apps/web/canvas/renderer-konva/text-properties.js'
import { applyCommand, invertCommand, type CanvasObj } from '../apps/web/canvas/state/commands.js'
import { resetCompositionFontCache } from '../apps/web/canvas/state/composition-font.js'
import { transformLayerGroup } from '../apps/web/canvas/state/layer-group.js'

afterEach(() => {
  cleanup()
  resetCompositionFontCache()
})

test('select text, edit fields, save and reload', async () => {
  const original = globalThis.fetch
  globalThis.fetch = (async () => {
    throw new Error('offline')
  }) as typeof fetch
  const initial: CanvasObj = {
    id: 'title',
    kind: 'text',
    x: 8,
    y: 6,
    width: 200,
    height: 48,
    text: '月圆人团圆',
    fontSize: 64,
    fontFamily: 'noto-sans-sc',
    fill: '#112233',
    textAlign: 'center',
    region: 'title',
    layerRole: 'text',
    lineBoxes: [{ text: '月圆人团圆', x: 0, y: 0, width: 180, height: 40 }],
  }
  const doc = {
    objects: {
      title: initial,
      group: {
        id: 'group',
        kind: 'board' as const,
        x: 0,
        y: 0,
        width: 100,
        height: 100,
        groupId: 'group',
      },
    },
    order: ['group', 'title'],
  }
  doc.objects.title.groupId = 'group'
  function Harness({
    source,
  }: {
    source: { objects: Record<string, CanvasObj>; order: string[] }
  }) {
    const [object, setObject] = useState(source.objects.title)
    return (
      <div>
        <TextPropertyBar
          object={object}
          onPatch={(patch, prev) => {
            applyCommand(source, { type: 'updateObject', id: 'title', patch, prevPatch: prev })
            setObject({ ...source.objects.title })
          }}
        />
        <LayeredTextReadout object={object} />
      </div>
    )
  }
  const screen = render(<Harness source={doc} />)
  const alert = await screen.findByRole('alert')
  assert.match(alert.textContent ?? '', /未改用其他字体/)
  fireEvent.change(screen.getByLabelText('文案'), { target: { value: '中秋快乐' } })
  fireEvent.change(screen.getByLabelText('字体'), { target: { value: 'noto-sans-sc' } })
  fireEvent.change(screen.getByLabelText('对齐'), { target: { value: 'left' } })
  fireEvent.change(screen.getByLabelText('字号'), { target: { value: '72' } })
  const saved = JSON.parse(JSON.stringify(doc)) as {
    objects: Record<string, CanvasObj>
    order: string[]
  }
  assert.equal(saved.objects.title.text, '中秋快乐')
  assert.equal(saved.objects.title.textAlign, 'left')
  assert.equal(saved.objects.title.fontSize, 72)
  assert.equal(saved.objects.title.fontFamily, 'noto-sans-sc')
  const lines = saved.objects.title.lineBoxes ?? []
  assert.equal(lines.map((line) => line.text).join(''), '中秋快乐')
  assert.ok(lines.every((line) => line.width <= saved.objects.title.width + 1))
  cleanup()
  const reloaded = render(<Harness source={saved} />)
  assert.equal(reloaded.getByTestId('layered-text').textContent, '中秋快乐')
  const subject = {
    id: 'subject',
    kind: 'image' as const,
    x: 20,
    y: 30,
    width: 40,
    height: 40,
    groupId: 'group',
    layerRole: 'subject' as const,
  }
  saved.objects.subject = subject
  const move = transformLayerGroup(saved.objects, 'group', { dx: 10, dy: 4, scale: 2 })
  applyCommand(saved, move)
  assert.equal(saved.objects.group.x, 10)
  assert.equal(saved.objects.group.y, 4)
  assert.equal(saved.objects.title.x, 10 + 8 * 2)
  assert.equal(saved.objects.subject.x, 10 + 20 * 2)
  assert.equal(saved.objects.subject.y, 4 + 30 * 2)
  assert.equal(saved.objects.subject.width, 80)
  applyCommand(saved, invertCommand(move))
  assert.equal(saved.objects.title.x, 8)
  assert.equal(saved.objects.subject.x, 20)
  assert.equal(saved.objects.group.width, 100)
  globalThis.fetch = original
})
