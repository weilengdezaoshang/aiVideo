import test from 'node:test'
import assert from 'node:assert/strict'
import { createCutoutSession } from '../apps/web/canvas/flows/cutout.js'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'
import { createInteraction, type MaskHandlers } from '../apps/web/canvas/engine/interaction.js'

test('识别后点击空白退出，再次抠图请求新识别，旧本机草稿不覆盖新选区', async () => {
  const drawn: string[] = []
  class Element extends EventTarget {
    hidden = true
    dataset: Record<string, string> = {}
    style = {}
    classList = { toggle() {} }
    offsetWidth = 100
    offsetHeight = 40
    offsetTop = 100
    width = 2
    height = 2
    value = '10'
    setAttribute() {}
    append() {}
    getContext() {
      return {
        clearRect() {},
        drawImage(image: { src: string }) {
          drawn.push(image.src)
        },
        getImageData: () => ({ data: new Uint8ClampedArray(16), width: 2, height: 2 }),
        createImageData: () => ({ data: new Uint8ClampedArray(16), width: 2, height: 2 }),
        putImageData() {},
      }
    }
  }
  class LoadedImage {
    onload: (() => void) | null = null
    onerror: (() => void) | null = null
    naturalWidth = 2
    naturalHeight = 2
    private url = ''
    get src() {
      return this.url
    }
    set src(value: string) {
      this.url = value
      queueMicrotask(() => this.onload?.())
    }
  }
  class Layer {
    add() {}
    destroy() {}
    batchDraw() {}
    draw() {}
  }
  class Overlay {
    image() {}
  }
  const elements = new Map<string, Element>()
  const doc = Object.assign(new EventTarget(), {
    body: new Element(),
    createElement: () => new Element(),
    querySelector: (selector: string) => {
      if (!elements.has(selector)) {
        elements.set(selector, new Element())
      }
      return elements.get(selector)
    },
  })
  const operations: string[] = []
  let storageCalls = 0
  const replacements = {
    document: doc,
    window: Object.assign(new EventTarget(), { innerWidth: 1000, innerHeight: 800 }),
    Image: LoadedImage,
    Konva: { Layer, Image: Overlay },
    requestAnimationFrame: () => 1,
    cancelAnimationFrame() {},
    localStorage: {
      getItem() {
        storageCalls++
        return 'data:image/png;base64,old-draft'
      },
      setItem() {
        storageCalls++
      },
      removeItem() {
        storageCalls++
      },
    },
    fetch: async (url: string) => {
      assert.ok(url.startsWith('/api/cutout/detect/asset-a?'))
      const operationId = new URL(url, 'http://localhost').searchParams.get('operationId')!
      operations.push(operationId)
      return Response.json({ operationId, urls: { original: `/mask-${operations.length}.png` } })
    },
  }
  const previous = new Map(
    Object.keys(replacements).map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]),
  )
  for (const [key, value] of Object.entries(replacements)) {
    Object.defineProperty(globalThis, key, { configurable: true, value })
  }
  const object = {
    id: 'image-a',
    kind: 'image' as const,
    assetId: 'asset-a',
    ext: 'png',
    x: 0,
    y: 0,
    width: 20,
    height: 20,
  }
  const store = createDocStore({
    id: 'doc',
    name: 'test',
    objects: { [object.id]: object },
    order: [object.id],
  })
  const interaction = createInteraction()
  const handlers: { mask: MaskHandlers | null } = { mask: null }
  // Only the used engine surface is represented by this DOM-free test adapter.
  const engine = {
    interaction,
    input: {
      setMaskHandlers(next: MaskHandlers | null) {
        handlers.mask = next
      },
      setRecognitionCancel() {},
      setRecognitionPointer() {},
    },
    stage: {
      add() {},
      container: () => ({
        getBoundingClientRect: () => ({ left: 0, top: 0, right: 1000, bottom: 800 }),
      }),
    },
    getCam: () => ({ x: 0, y: 0, scale: 1 }),
    worldToScreen: (point: { x: number; y: number }) => point,
    screenToWorld: (point: { x: number; y: number }) => point,
    registerCamLayer() {},
    unregisterCamLayer() {},
  } as unknown as Parameters<typeof createCutoutSession>[0]['engine']
  let session: ReturnType<typeof createCutoutSession> | undefined
  try {
    session = createCutoutSession({ getDocStore: () => store, engine })
    await session.open(object)
    assert.equal(interaction.getState().mode, 'mask')
    assert.equal(drawn.at(-1), '/mask-1.png')
    handlers.mask?.beforeDown?.({ clientX: 600, clientY: 500 } as PointerEvent)
    assert.equal(interaction.getState().mode, 'select')
    await session.open(object)
    assert.equal(interaction.getState().mode, 'mask')
    assert.equal(operations.length, 2)
    assert.notEqual(operations[0], operations[1])
    assert.equal(drawn.at(-1), '/mask-2.png')
    assert.equal(storageCalls, 0)
  } finally {
    session?.exit()
    for (const [key, descriptor] of previous) {
      if (descriptor) {
        Object.defineProperty(globalThis, key, descriptor)
      } else {
        Reflect.deleteProperty(globalThis, key)
      }
    }
  }
})
