import test from 'node:test'
import assert from 'node:assert/strict'
import { initContextMenu } from '../apps/web/canvas/flows/context-menu.js'
import { createInteraction } from '../apps/web/canvas/engine/interaction.js'

test('右键菜单在手势开始时关闭，拖动中禁止重开，结束后必须再次右键', () => {
  const menu = {
    hidden: true,
    style: {},
    getBoundingClientRect: () => ({ width: 200, height: 100 }),
  }
  const previous = ['window', 'document'].map((key) =>
    Object.getOwnPropertyDescriptor(globalThis, key),
  )
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: {
      innerWidth: 1000,
      innerHeight: 800,
      addEventListener() {},
    },
  })
  Object.defineProperty(globalThis, 'document', {
    configurable: true,
    value: {
      querySelector: (id: string) => (id === '#context-menu' ? menu : null),
    },
  })
  try {
    const interaction = createInteraction()
    let open = () => {}
    let selections = 0
    let reportedOpen = false
    initContextMenu({
      getDocStore: () => ({
        doc: {
          objects: {
            image: { id: 'image', kind: 'image' as const, x: 0, y: 0, width: 100, height: 100 },
          },
        },
      }),
      onOpenChange: (open) => {
        reportedOpen = open
      },
    }).attach(
      {
        interaction,
        stage: {
          on: (_name: string, handler: (event: unknown) => void) => {
            open = () => handler({ evt: { preventDefault() {}, clientX: 100, clientY: 100 } })
          },
          container: () => ({ getBoundingClientRect: () => ({ left: 0, top: 0 }) }),
        },
        contentLayer: { getIntersection: () => ({ id: () => 'image' }) },
      },
      () => ({
        setSelected() {
          selections++
        },
        deleteSelected() {},
      }),
    )
    for (const button of [0, 1]) {
      open()
      assert.equal(menu.hidden, false)
      assert.equal(reportedOpen, true)
      const before = selections
      interaction.begin(button)
      assert.equal(menu.hidden, true)
      assert.equal(reportedOpen, false)
      open()
      assert.equal(menu.hidden, true)
      assert.equal(selections, before)
      interaction.end()
      assert.equal(menu.hidden, true)
    }
    open()
    assert.equal(menu.hidden, false)
    interaction.setMode('mask', 'image')
    open()
    assert.equal(menu.hidden, true)
  } finally {
    ;['window', 'document'].forEach((key, index) => {
      const descriptor = previous[index]
      if (descriptor) {
        Object.defineProperty(globalThis, key, descriptor)
      } else {
        Reflect.deleteProperty(globalThis, key)
      }
    })
  }
})
