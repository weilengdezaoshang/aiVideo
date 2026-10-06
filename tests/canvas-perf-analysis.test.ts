import assert from 'node:assert/strict'
import test from 'node:test'
import {
  panTrajectory,
  percentile,
  validateRun,
  comparisonProblems,
  type ComparisonKey,
} from '../apps/web/canvas/perf/analysis.js'
import type { BrowserSample } from '../apps/web/canvas/perf/protocol.js'

function sample(): BrowserSample {
  const state = {
    documentId: 'doc',
    objectCount: 500,
    selectionCount: 0,
    camera: { x: 0, y: 0, scale: 2 },
    viewport: { width: 800, height: 600 },
  }
  return {
    startMs: 0,
    elapsedMs: 3000,
    frameIntervals: [16, 20, 100],
    inputs: [],
    events: [],
    longTasks: [],
    visibilityChanges: 0,
    hiddenAtStart: false,
    supportedEntries: [],
    initial: state,
    final: { ...state, camera: { x: -60, y: -30, scale: 2 } },
    counters: null,
    functions: null,
    heapBefore: null,
    heapAfter: null,
  }
}
test('trajectory has a fixed event count, duration and endpoint independent of rAF', () => {
  const events = panTrajectory(10, 20)
  assert.equal(events.length, 64)
  assert.equal(events.filter((e) => e.kind === 'move').length, 60)
  assert.deepEqual(events.at(-1), { atMs: 3160, kind: 'space-up', x: 130, y: 80 })
  assert.ok(events.every((e, i) => i === 0 || e.atMs >= events[i - 1].atMs))
})
test('invalid input workloads and wrong world-coordinate outcomes are rejected', () => {
  const browser = sample()
  const events = panTrajectory(10, 20)
  const records = events.map((e) => ({ ...e, sentMs: e.atMs, ackMs: e.atMs + 1 }))
  browser.inputs = events.map((e) => ({
    type:
      e.kind === 'move'
        ? 'mousemove'
        : e.kind === 'down'
          ? 'mousedown'
          : e.kind === 'up'
            ? 'mouseup'
            : e.kind.endsWith('down')
              ? 'keydown'
              : 'keyup',
    trusted: true,
    atMs: e.atMs,
    x: e.x,
    y: e.y,
  }))
  assert.deepEqual(validateRun(browser, records, 'pan', 500), [])
  browser.inputs.splice(2, 1)
  assert.deepEqual(
    validateRun(browser, records, 'pan', 500),
    [],
    'ordered intermediate coalescing is valid for a fixed offered stream',
  )
  assert.ok(
    validateRun(browser, records, 'pan-work', 500).some((v) => v.includes('mousemove count')),
    'the same missing move is invalid for exact work',
  )
  browser.inputs.pop()
  assert.ok(validateRun(browser, records, 'pan', 500).some((v) => v.includes('keyup count')))
  browser.final.camera.x = 60
  assert.ok(validateRun(browser, records, 'pan', 500).includes('pan outcome mismatch'))
})
test('late schedule, background page, empty samples and failed selection are explicit', () => {
  const browser = sample()
  browser.hiddenAtStart = true
  browser.frameIntervals = []
  const issues = validateRun(
    browser,
    [{ kind: 'move', atMs: 0, sentMs: 101, ackMs: 300, x: 0, y: 0 }],
    'select-all',
    500,
  )
  assert.ok(issues.some((v) => v.startsWith('input schedule missed')))
  assert.ok(issues.includes('select-all outcome mismatch'))
  assert.ok(issues.includes('no rAF samples'))
  assert.ok(issues.some((v) => v.includes('hidden')))
})
test('unsupported measurements stay null and incompatible DPR is not compared', () => {
  assert.equal(percentile([], 0.95), null)
  assert.equal(percentile([30, 10, 20], 0.95), 30)
  const identity: ComparisonKey = {
    protocol: 'v2',
    sceneHash: 'hash',
    sceneName: '500',
    action: 'pan',
    mode: 'clean',
    browser: 'Chrome',
    width: 800,
    height: 600,
    dpr: 1,
    platform: 'test',
    cachePolicy: 'settled',
    trajectoryHash: 'path',
    initialStateHash: 'state',
  }
  assert.deepEqual(comparisonProblems(identity, { ...identity, dpr: 2 }), ['dpr differs'])
})
