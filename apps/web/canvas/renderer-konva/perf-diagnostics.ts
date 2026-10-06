import Konva from 'konva'
import type { FunctionSample } from '../perf/protocol.js'

/** Exact calls and inclusive wall time, only in an explicitly diagnostic benchmark. */
export function exposeRendererDiagnostics(): () => void {
  const params = new URLSearchParams(location.search)
  if (params.get('benchmark') !== '1' || params.get('mode') !== 'diagnostic') {
    return () => {}
  }
  const rows: Record<string, FunctionSample> = {}
  const restore: Array<() => void> = []
  function wrap<Args extends unknown[], Result>(
    name: string,
    original: (this: Konva.Layer, ...args: Args) => Result,
  ): (this: Konva.Layer, ...args: Args) => Result {
    return function (this: Konva.Layer, ...args: Args) {
      const began = performance.now()
      try {
        return original.apply(this, args)
      } finally {
        const elapsed = performance.now() - began
        const row = (rows[name] ??= { calls: 0, totalMs: 0, maxMs: 0 })
        row.calls++
        row.totalMs += elapsed
        row.maxMs = Math.max(row.maxMs, elapsed)
      }
    }
  }
  const scene = Konva.Layer.prototype.drawScene
  const hit = Konva.Layer.prototype.drawHit
  const wrappedScene = wrap('Konva.Layer.drawScene', scene)
  const wrappedHit = wrap('Konva.Layer.drawHit', hit)
  Konva.Layer.prototype.drawScene = wrappedScene
  Konva.Layer.prototype.drawHit = wrappedHit
  restore.push(() => {
    if (Konva.Layer.prototype.drawScene === wrappedScene) {
      Konva.Layer.prototype.drawScene = scene
    }
    if (Konva.Layer.prototype.drawHit === wrappedHit) {
      Konva.Layer.prototype.drawHit = hit
    }
  })
  const api = {
    reset() {
      for (const key of Object.keys(rows)) {
        delete rows[key]
      }
    },
    snapshot: () =>
      Object.fromEntries(Object.entries(rows).map(([key, value]) => [key, { ...value }])),
  }
  window.__canvasBenchmarkFunctions = api
  return () => {
    for (const cleanup of restore) {
      cleanup()
    }
    if (window.__canvasBenchmarkFunctions === api) {
      delete window.__canvasBenchmarkFunctions
    }
  }
}
