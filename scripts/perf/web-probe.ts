import type { BenchmarkState } from '../../apps/web/canvas/perf/protocol.js'

interface LCPEntry extends PerformanceEntry {
  size: number
  element: Element | null
  url: string
}
interface ShiftEntry extends PerformanceEntry {
  value: number
  hadRecentInput: boolean
}
export interface WebSample {
  fcpMs: number | null
  lcpObservedMs: number | null
  lcpElement: string | null
  clsObserved: number
  domReadyProxyMs: number | null
  fontReadyMs: number | null
  observedUntilMs: number
  longTaskCount: number
  longTaskTotalMs: number
  longTaskBlockingMs: number
  navigation: Record<string, number> | null
  resources: {
    url: string
    type: string
    transfer: number
    encoded: number
    decoded: number
    duration: number
  }[]
  canvas: BenchmarkState | null
  supportedEntries: string[]
  hidden: boolean
}
declare global {
  interface Window {
    __webLoadProbe?: { snapshot(): WebSample }
  }
}
let lcp: LCPEntry | null = null
const shifts: ShiftEntry[] = []
const tasks: PerformanceEntry[] = []
function observe(type: string, callback: (entries: PerformanceEntry[]) => void) {
  if (PerformanceObserver.supportedEntryTypes.includes(type)) {
    new PerformanceObserver((list) => callback(list.getEntries())).observe({ type, buffered: true })
  }
}
observe('largest-contentful-paint', (entries) => {
  lcp = entries.at(-1) as LCPEntry
})
observe('layout-shift', (entries) => {
  shifts.push(...(entries as ShiftEntry[]))
})
observe('longtask', (entries) => {
  tasks.push(...entries)
})
performance.setResourceTimingBufferSize(2000)
let ready: number | null = null
let fontReady: number | null = null
void document.fonts.ready.then(() => {
  fontReady = performance.now()
})
let hidden = document.hidden
addEventListener('visibilitychange', () => {
  hidden ||= document.hidden
})
function poll() {
  const path = location.pathname
  const element =
    path === '/'
      ? document.querySelector('#hero-title')
      : path.startsWith('/workspace')
        ? document.querySelector('#workspace-main .recent-section')
        : document.querySelector('.konvajs-content canvas')
  const canvas = window.__canvasBenchmark?.state()
  const complete =
    path === '/'
      ? Boolean(element)
      : path.startsWith('/workspace')
        ? Boolean(
            element &&
            !document.querySelector('.project-skeleton') &&
            document.querySelector('textarea'),
          )
        : Boolean(element && canvas?.objectCount === 500 && canvas.viewport.width > 0)
  if (complete && element && element.getBoundingClientRect().width > 0) {
    requestAnimationFrame(() => {
      ready = performance.now()
    })
  } else {
    requestAnimationFrame(poll)
  }
}
requestAnimationFrame(poll)
window.__webLoadProbe = {
  snapshot() {
    let max = 0,
      session = 0,
      first = 0,
      last = 0
    for (const shift of shifts.filter((entry) => !entry.hadRecentInput)) {
      if (shift.startTime - last > 1000 || shift.startTime - first > 5000) {
        first = shift.startTime
        session = 0
      }
      session += shift.value
      last = shift.startTime
      max = Math.max(max, session)
    }
    const nav = performance.getEntriesByType('navigation')[0] as
      PerformanceNavigationTiming | undefined
    return {
      fcpMs: performance.getEntriesByName('first-contentful-paint')[0]?.startTime ?? null,
      lcpObservedMs: lcp?.startTime ?? null,
      lcpElement: lcp?.element
        ? `${lcp.element.tagName.toLowerCase()}#${lcp.element.id}.${lcp.element.className}`
        : null,
      clsObserved: max,
      domReadyProxyMs: ready,
      fontReadyMs: fontReady,
      observedUntilMs: performance.now(),
      longTaskCount: tasks.length,
      longTaskTotalMs: tasks.reduce((sum, task) => sum + task.duration, 0),
      longTaskBlockingMs: tasks.reduce((sum, task) => sum + Math.max(0, task.duration - 50), 0),
      navigation: nav
        ? {
            ttfbMs: nav.responseStart - nav.startTime,
            domContentLoadedMs: nav.domContentLoadedEventEnd,
            loadMs: nav.loadEventEnd,
            transfer: nav.transferSize,
            encoded: nav.encodedBodySize,
            decoded: nav.decodedBodySize,
          }
        : null,
      resources: (performance.getEntriesByType('resource') as PerformanceResourceTiming[]).map(
        (entry) => ({
          url: entry.name,
          type: entry.initiatorType,
          transfer: entry.transferSize,
          encoded: entry.encodedBodySize,
          decoded: entry.decodedBodySize,
          duration: entry.duration,
        }),
      ),
      canvas: window.__canvasBenchmark?.state() ?? null,
      supportedEntries: [...PerformanceObserver.supportedEntryTypes],
      hidden,
    }
  },
}
