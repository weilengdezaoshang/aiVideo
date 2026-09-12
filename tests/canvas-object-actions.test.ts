import test from 'node:test'
import assert from 'node:assert/strict'
import { initObjectActions } from '../apps/web/canvas/flows/object-actions.js'
import { createInteraction } from '../apps/web/canvas/engine/interaction.js'

// 只模拟工具栏需要的 DOM；验证识别态切换与摄像机变化后的实际位置。
test('抠图期间浮层始终跟随源图，退出后恢复普通菜单', () => {
  const elements = new Map<string, any>() // eslint-disable-line @typescript-eslint/no-explicit-any
  for (const id of [
    'obj-actions',
    'obj-add-chat',
    'obj-cutout',
    'obj-download',
    'cutout-bar',
    'inpaint-bar',
  ]) {
    elements.set(`#${id}`, {
      hidden: true,
      dataset: {},
      style: {},
      offsetWidth: 300,
      offsetHeight: 48,
      addEventListener() {},
    })
  }
  const listeners = new Map<string, () => void>()
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'document')
  Object.defineProperty(globalThis, 'document', {
    configurable: true,
    value: {
      querySelector: (id: string) => elements.get(id),
      addEventListener: (name: string, fn: () => void) => listeners.set(name, fn),
    },
  })
  try {
    let offset = 0
    const interaction = createInteraction()
    const actions = initObjectActions({
      engine: {
        interaction,
        getCam: () => ({ x: 0, y: 0, scale: 1 }),
        worldToScreen: ({ x, y }: { x: number; y: number }) => ({ x: x + offset, y }),
        panBy() {},
      },
      rootEl: { clientWidth: 1000, clientHeight: 800 } as HTMLElement,
      getDocStore: () => ({
        doc: {
          objects: {
            source: { id: 'source', kind: 'image', x: 200, y: 100, width: 300, height: 200 },
          },
        },
      }),
      getSelection: () => ['source'],
      onAddToChat() {},
      onCutout() {},
    })
    actions.refresh()
    assert.equal(elements.get('#obj-actions').hidden, false)
    actions.setContextMenuOpen(true)
    assert.equal(elements.get('#obj-actions').hidden, true)
    actions.refresh({ pan: true })
    assert.equal(elements.get('#obj-actions').hidden, true)
    actions.setContextMenuOpen(false)
    assert.equal(elements.get('#obj-actions').hidden, false)
    // 与 app 的状态订阅一致；拖动中的相机/文档刷新不能重新弹出菜单。
    const unsubscribe = interaction.subscribe(() => actions.refresh())
    for (const button of [0, 1]) {
      interaction.begin(button)
      assert.equal(elements.get('#obj-actions').hidden, true)
      offset = 40
      actions.refresh({ pan: true })
      assert.equal(elements.get('#obj-actions').hidden, true)
      interaction.end()
      assert.equal(elements.get('#obj-actions').hidden, false)
      assert.equal(elements.get('#obj-actions').style.left, '240px')
    }
    interaction.setMode('pan')
    assert.equal(elements.get('#obj-actions').hidden, false)
    interaction.begin(0)
    interaction.cancel()
    assert.equal(elements.get('#obj-actions').hidden, false)
    interaction.setMode('select')
    unsubscribe()
    offset = 0
    const cutout = elements.get('#cutout-bar')
    cutout.hidden = false
    cutout.dataset.sourceId = 'source'
    listeners.get('gencanvas:maskmode')!()
    assert.equal(elements.get('#obj-actions').hidden, true)
    assert.equal(cutout.style.top, '310px')
    assert.equal(cutout.style.left, '200px')
    offset = 80
    actions.refresh()
    assert.equal(cutout.style.left, '280px')
    cutout.hidden = true
    listeners.get('gencanvas:maskmode')!()
    assert.equal(elements.get('#obj-actions').hidden, false)
  } finally {
    if (previous) {
      Object.defineProperty(globalThis, 'document', previous)
    } else {
      Reflect.deleteProperty(globalThis, 'document')
    }
  }
})
