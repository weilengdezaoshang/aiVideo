import { readFile, readdir } from 'node:fs/promises'
import { join } from 'node:path'
import type { Run } from './web-load.js'

const [beforeDir, afterDir] = process.argv.slice(2)
if (!beforeDir || !afterDir) {
  throw new Error('Usage: web-compare.ts BASELINE_DIR CANDIDATE_DIR')
}
interface Environment {
  protocol: string
  settings: Record<string, unknown>
  browser: { product: string; revision: string }
  node: string
  platform: string
  cpu: string
  ram: number
  collectorHash: string
  fixture: { scenes: { hash: string }[] }
}
async function environment(dir: string): Promise<Environment> {
  return JSON.parse(await readFile(join(dir, 'environment.json'), 'utf8')) as Environment
}
const beforeEnv = await environment(beforeDir)
const afterEnv = await environment(afterDir)
for (const key of [
  'protocol',
  'settings',
  'browser',
  'node',
  'platform',
  'cpu',
  'ram',
  'collectorHash',
  'fixture',
] as const) {
  if (JSON.stringify(beforeEnv[key]) !== JSON.stringify(afterEnv[key])) {
    throw new Error(`Incompatible ${key}; do not calculate gains`)
  }
}
if (beforeEnv.protocol !== 'web-load-v1') {
  throw new Error('Unsupported protocol')
}
async function load(dir: string): Promise<Run[]> {
  const rows: Run[] = []
  for (const file of (await readdir(dir)).filter((name) => /-(cold|warm)-\d+\.json$/.test(name))) {
    rows.push(JSON.parse(await readFile(join(dir, file), 'utf8')) as Run)
  }
  return rows
}
const before = await load(beforeDir)
const after = await load(afterDir)
const groups = new Set([...before, ...after].map((row) => `${row.page}/${row.cache}`))
function median(values: number[]) {
  const sorted = [...values].sort((a, b) => a - b)
  return sorted[Math.floor(sorted.length / 2)]
}
const metrics: [string, (run: Run) => number | null][] = [
  ['networkBytes', (run) => run.networkBytes],
  ['jsNetworkBytes', (run) => run.jsNetworkBytes],
  ['FCP ms', (run) => run.sample.fcpMs],
  ['LCP observed ms', (run) => run.sample.lcpObservedMs],
  ['DOM ready proxy ms', (run) => run.sample.domReadyProxyMs],
  ['CLS observed', (run) => run.sample.clsObserved],
  ['long task blocking ms', (run) => run.sample.longTaskBlockingMs],
]
const rows = []
for (const group of groups) {
  const eligible = (runs: Run[]) =>
    runs.filter((run) => `${run.page}/${run.cache}` === group && run.issues.length === 0)
  for (const [metric, fn] of metrics) {
    const values = (runs: Run[]) =>
      eligible(runs)
        .map(fn)
        .filter((value): value is number => value !== null)
    const a = values(before),
      b = values(after)
    if (a.length < 3 || b.length < 3) {
      rows.push({
        group,
        metric,
        status: 'insufficient valid samples',
        beforeN: a.length,
        afterN: b.length,
      })
      continue
    }
    const baseline = median(a),
      candidate = median(b)
    rows.push({
      group,
      metric,
      baseline,
      candidate,
      saved: baseline - candidate,
      savedPercent: baseline ? (((baseline - candidate) / baseline) * 100).toFixed(2) : 'n/a',
      beforeRange: `${Math.min(...a).toFixed(2)}–${Math.max(...a).toFixed(2)}`,
      afterRange: `${Math.min(...b).toFixed(2)}–${Math.max(...b).toFixed(2)}`,
    })
  }
}
console.table(rows)
console.log(
  'Observed lab medians and ranges; 3 runs do not establish statistical significance or real-user performance.',
)
