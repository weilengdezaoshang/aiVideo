import type { BrowserSample } from './protocol.js'

export type ScheduledInput = {
  atMs: number
  kind: 'move' | 'down' | 'up' | 'space-down' | 'space-up' | 'select-all-down' | 'select-all-up'
  x: number
  y: number
}
export function panTrajectory(
  x: number,
  y: number,
  durationMs = 3000,
  moves = 60,
): ScheduledInput[] {
  const result: ScheduledInput[] = [
    { atMs: 0, kind: 'space-down', x, y },
    { atMs: 40, kind: 'down', x, y },
  ]
  for (let i = 1; i <= moves; i++) {
    result.push({
      atMs: 80 + (durationMs * i) / moves,
      kind: 'move',
      x: x + (120 * i) / moves,
      y: y + (60 * i) / moves,
    })
  }
  result.push(
    { atMs: durationMs + 120, kind: 'up', x: x + 120, y: y + 60 },
    { atMs: durationMs + 160, kind: 'space-up', x: x + 120, y: y + 60 },
  )
  return result
}
export function percentile(values: number[], fraction: number): number | null {
  if (!values.length) {
    return null
  }
  const sorted = [...values].sort((a, b) => a - b)
  return sorted[Math.max(0, Math.ceil(sorted.length * fraction) - 1)]
}
export function summarize(sample: BrowserSample) {
  return {
    rafFps: (sample.frameIntervals.length * 1000) / sample.elapsedMs,
    frameP95Ms: percentile(sample.frameIntervals, 0.95),
    frameMaxMs: sample.frameIntervals.length ? Math.max(...sample.frameIntervals) : null,
    longTaskCount: sample.supportedEntries.includes('longtask') ? sample.longTasks.length : null,
    longTaskMs: sample.supportedEntries.includes('longtask')
      ? sample.longTasks.reduce(
          (sum, entry) =>
            sum +
            Math.max(
              0,
              Math.min(entry.startTime + entry.duration, sample.startMs + sample.elapsedMs) -
                entry.startTime,
            ),
          0,
        )
      : null,
    // Event Timing is thresholded and quantized; not field INP.
    observedEventMaxMs: sample.events.length
      ? Math.max(...sample.events.map((e) => e.duration))
      : null,
    inputCount: sample.inputs.length,
    trustedInputCount: sample.inputs.filter((e) => e.trusted).length,
    heapDeltaBytes:
      sample.heapBefore === null || sample.heapAfter === null
        ? null
        : sample.heapAfter - sample.heapBefore,
  }
}
export type DispatchRecord = {
  kind: ScheduledInput['kind']
  atMs: number
  sentMs: number
  ackMs: number
  x: number
  y: number
}
export type ComparisonKey = {
  protocol: string
  sceneHash: string
  sceneName: string
  action: string
  mode: string
  browser: string
  width: number
  height: number
  dpr: number
  platform: string
  cachePolicy: string
  trajectoryHash: string
  initialStateHash: string
}
export function comparisonProblems(before: ComparisonKey, after: ComparisonKey): string[] {
  return (Object.keys(before) as (keyof ComparisonKey)[])
    .filter((key) => before[key] !== after[key])
    .map((key) => `${key} differs`)
}
export function validateRun(
  sample: BrowserSample,
  dispatch: DispatchRecord[],
  action: 'pan' | 'pan-work' | 'select-all' | 'idle',
  expectedObjects: number,
  deadlineToleranceMs = 100,
): string[] {
  const issues: string[] = []
  if (sample.hiddenAtStart || sample.visibilityChanges) {
    issues.push('page visibility changed or started hidden')
  }
  if (
    sample.initial.objectCount !== expectedObjects ||
    sample.final.objectCount !== expectedObjects
  ) {
    issues.push('object workload mismatch')
  }
  if (sample.initial.documentId !== sample.final.documentId) {
    issues.push('document changed during sampling')
  }
  if (
    sample.initial.viewport.width !== sample.final.viewport.width ||
    sample.initial.viewport.height !== sample.final.viewport.height
  ) {
    issues.push('canvas viewport changed during sampling')
  }
  if (sample.inputs.some((event) => !event.trusted)) {
    issues.push('untrusted browser input')
  }
  const expectedTypes = dispatch.map((event) =>
    event.kind === 'move'
      ? 'mousemove'
      : event.kind === 'down'
        ? 'mousedown'
        : event.kind === 'up'
          ? 'mouseup'
          : event.kind.endsWith('down')
            ? 'keydown'
            : 'keyup',
  )
  for (const type of ['mousemove', 'mousedown', 'mouseup', 'keydown', 'keyup']) {
    if (type === 'mousemove' && action === 'pan') {
      continue
    }
    if (
      sample.inputs.filter((e) => e.type === type).length !==
      expectedTypes.filter((t) => t === type).length
    ) {
      issues.push(`received ${type} count differs from scheduled workload`)
    }
  }
  if (
    action !== 'pan-work' &&
    dispatch.some((event) => event.sentMs - event.atMs > deadlineToleranceMs)
  ) {
    issues.push('input schedule missed deadline; throughput stress result only')
  }
  if (action === 'pan' || action === 'pan-work') {
    const expectedMoves = dispatch.filter((event) => event.kind === 'move')
    const observedMoves = sample.inputs.filter((event) => event.type === 'mousemove')
    if (observedMoves.length === 0 || observedMoves.length > expectedMoves.length) {
      issues.push('received move count invalid')
    }
    let index = 0
    for (const observed of observedMoves) {
      while (
        index < expectedMoves.length &&
        (Math.abs((observed.x ?? Infinity) - expectedMoves[index].x) > 1 ||
          Math.abs((observed.y ?? Infinity) - expectedMoves[index].y) > 1)
      ) {
        index++
      }
      if (index === expectedMoves.length) {
        issues.push('observed movement is not an ordered subset of offered trajectory')
        break
      }
      index++
    }
    const { initial, final } = sample
    if (
      Math.abs(final.camera.x - initial.camera.x + 120 / initial.camera.scale) > 1 ||
      Math.abs(final.camera.y - initial.camera.y + 60 / initial.camera.scale) > 1 ||
      final.camera.scale !== initial.camera.scale
    ) {
      issues.push('pan outcome mismatch')
    }
  }
  if (action === 'select-all' && sample.final.selectionCount !== expectedObjects) {
    issues.push('select-all outcome mismatch')
  }
  if (!sample.frameIntervals.length) {
    issues.push('no rAF samples')
  }
  return issues
}
