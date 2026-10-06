import type { BrowserSample, EventSample, InputSample } from './protocol.js'

type TimingEntry = PerformanceEntry & {
  interactionId: number
  processingStart: number
  processingEnd: number
}
type HeapPerformance = Performance & { memory?: { usedJSHeapSize: number } }
const heap = () => (performance as HeapPerformance).memory?.usedJSHeapSize ?? null
let active = false
let frame = 0
let previous = 0
let startMs = 0
let initial: BrowserSample['initial']
let heapBefore: number | null = null
let initialCounters: Record<string, number> | null = null
let hiddenAtStart = false
let visibilityChanges = 0
let frames: number[] = []
let inputs: InputSample[] = []
let events: EventSample[] = []
let longTasks: BrowserSample['longTasks'] = []
const observers: PerformanceObserver[] = []

function collect(type: string, entries: PerformanceEntry[]) {
  for (const entry of entries) {
    // Delayed observer delivery can contain records from before this action.
    if (entry.startTime < startMs) {
      continue
    }
    if (type === 'event') {
      const event = entry as TimingEntry
      events.push({
        name: event.name,
        interactionId: event.interactionId,
        startTime: event.startTime,
        duration: event.duration,
        inputDelay: event.processingStart - event.startTime,
        processing: event.processingEnd - event.processingStart,
      })
    } else {
      longTasks.push({ startTime: entry.startTime, duration: entry.duration })
    }
  }
}

for (const type of ['event', 'longtask']) {
  if (PerformanceObserver.supportedEntryTypes.includes(type)) {
    const observer = new PerformanceObserver((list) => {
      if (active) {
        collect(type, list.getEntries())
      }
    })
    observer.observe({ type, ...(type === 'event' ? { durationThreshold: 16 } : {}) })
    observers.push(observer)
  }
}
for (const name of ['mousedown', 'mousemove', 'mouseup', 'keydown', 'keyup']) {
  document.addEventListener(
    name,
    (event) => {
      if (!active) {
        return
      }
      const mouse = event instanceof MouseEvent ? event : null
      inputs.push({
        type: event.type,
        trusted: event.isTrusted,
        atMs: performance.now() - startMs,
        ...(mouse ? { x: mouse.clientX, y: mouse.clientY } : {}),
      })
    },
    { capture: true },
  )
}
document.addEventListener('visibilitychange', () => {
  if (active) {
    visibilityChanges++
  }
})

window.__canvasCollector = {
  receivedMoves: () => inputs.filter((event) => event.type === 'mousemove').length,
  ready: () =>
    Boolean(window.__canvasBenchmark && document.querySelector('.konvajs-content canvas')),
  start() {
    if (active || !window.__canvasBenchmark) {
      throw new Error('Collector already running or canvas not ready')
    }
    frames = []
    inputs = []
    events = []
    longTasks = []
    visibilityChanges = 0
    hiddenAtStart = document.hidden
    initial = window.__canvasBenchmark.state()
    initialCounters = window.__canvasBenchmark.counters()
    window.__canvasBenchmarkFunctions?.reset()
    heapBefore = heap()
    for (const observer of observers) {
      observer.takeRecords()
    }
    startMs = previous = performance.now()
    active = true
    function tick(now: number) {
      if (!active) {
        return
      }
      frames.push(now - previous)
      previous = now
      frame = requestAnimationFrame(tick)
    }
    frame = requestAnimationFrame(tick)
  },
  stop() {
    if (!active || !window.__canvasBenchmark) {
      throw new Error('Collector not running or canvas destroyed')
    }
    // Flush undelivered records before closing the window.
    for (const observer of observers) {
      const entries = observer.takeRecords()
      for (const entry of entries) {
        collect(entry.entryType, [entry])
      }
    }
    const elapsedMs = performance.now() - startMs
    active = false
    cancelAnimationFrame(frame)
    const counts = window.__canvasBenchmark.counters()
    return {
      startMs,
      elapsedMs,
      frameIntervals: frames,
      inputs,
      events,
      longTasks,
      visibilityChanges,
      hiddenAtStart,
      supportedEntries: [...PerformanceObserver.supportedEntryTypes],
      initial,
      final: window.__canvasBenchmark.state(),
      counters:
        counts && initialCounters
          ? Object.fromEntries(
              Object.entries(counts).map(([key, value]) => [
                key,
                value - (initialCounters?.[key] ?? 0),
              ]),
            )
          : null,
      heapBefore,
      heapAfter: heap(),
      functions: window.__canvasBenchmarkFunctions?.snapshot() ?? null,
    }
  },
}
