import { readFile, readdir, writeFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import {
  comparisonProblems,
  percentile,
  type ComparisonKey,
} from '../../apps/web/canvas/perf/analysis.js'

type Row = {
  label: string
  identity: ComparisonKey
  issues: string[]
  eligibleForCadenceComparison: boolean
  eligibleForWorkComparison: boolean
  completionProxyMs: number | null
  summary: { rafFps: number; frameP95Ms: number | null; observedEventMaxMs: number | null }
}
const [beforeDir, afterDir, destination] = process.argv.slice(2)
if (!beforeDir || !afterDir) {
  throw new Error('Usage: compare.ts BASELINE_DIR CANDIDATE_DIR [report.md]')
}
async function load(dir: string): Promise<Row[]> {
  const rows: Row[] = []
  for (const name of await readdir(dir)) {
    if (/-(idle|pan|pan-work|select-all)-\d+\.json$/.test(name)) {
      const row = JSON.parse(await readFile(join(dir, name), 'utf8')) as Row
      if (row.identity?.protocol !== 'canvas-native-v3') {
        throw new Error(`Unsupported result: ${name}`)
      }
      rows.push(row)
    }
  }
  return rows
}
const baseline = await load(beforeDir)
const candidate = await load(afterDir)
const beforeEnv = JSON.parse(await readFile(join(beforeDir, 'environment.json'), 'utf8')) as {
  cpu: string
  ramBytes: number
  collectorHash: string
  headless: boolean
  sourceHashes: Record<string, string>
}
const afterEnv = JSON.parse(
  await readFile(join(afterDir, 'environment.json'), 'utf8'),
) as typeof beforeEnv
const buildHash = (env: typeof beforeEnv) => env.sourceHashes['.web-build/canvas/app.js']
const sameBuild = Boolean(buildHash(beforeEnv) && buildHash(beforeEnv) === buildHash(afterEnv))
const environmentIssues = (['cpu', 'ramBytes', 'collectorHash', 'headless'] as const)
  .filter((key) => beforeEnv[key] !== afterEnv[key])
  .map((key) => `${key} differs`)
const groups = new Map<string, { before: Row[]; after: Row[] }>()
for (const [side, rows] of [
  ['before', baseline],
  ['after', candidate],
] as const) {
  for (const row of rows) {
    const key = `${row.identity.sceneName}/${row.identity.action}/${row.identity.mode}`
    const group = groups.get(key) ?? { before: [], after: [] }
    group[side].push(row)
    groups.set(key, group)
  }
}
const lines = [
  '# Canvas benchmark comparison',
  '',
  `Baseline: ${resolve(beforeDir)}`,
  `Candidate: ${resolve(afterDir)}`,
  '',
  'Numbers below are measured medians of eligible runs, not architecture forecasts.',
  'rAF FPS is a frame-progression proxy; completion uses two rAF opportunities and CDP overhead, not GPU presentation.',
  'Diagnostic mode is for attribution only; no user-experience gain percentage is calculated from diagnostic runs.',
  ...(sameBuild
    ? [
        'Both directories contain the same app build: differences are A/A noise, not optimization gains.',
      ]
    : []),
  '',
  '| Scenario | Metric | Before | After | Change | Eligible samples |',
  '|---|---|---:|---:|---:|---|',
]
for (const [key, group] of groups) {
  if (!group.before.length || !group.after.length) {
    lines.push(
      `| ${key} | missing side | — | — | withheld | ${group.before.length}/${group.after.length} |`,
    )
    continue
  }
  const incompatible = [
    ...environmentIssues,
    ...comparisonProblems(group.before[0].identity, group.after[0].identity),
  ]
  for (const row of [...group.before, ...group.after]) {
    incompatible.push(...comparisonProblems(group.before[0].identity, row.identity))
  }
  if (incompatible.length) {
    lines.push(
      `| ${key} | incompatible: ${[...new Set(incompatible)].join(', ')} | — | — | withheld | — |`,
    )
    continue
  }
  const metrics = [
    { name: 'rAF FPS', higher: true, cadence: true, get: (r: Row) => r.summary.rafFps },
    {
      name: 'frame interval P95 ms',
      higher: false,
      cadence: true,
      get: (r: Row) => r.summary.frameP95Ms,
    },
    {
      name: 'fixed-work completion proxy ms',
      higher: false,
      cadence: false,
      get: (r: Row) => r.completionProxyMs,
    },
    {
      name: 'observed event max ms',
      higher: false,
      cadence: true,
      get: (r: Row) => r.summary.observedEventMaxMs,
    },
  ]
  for (const metric of metrics) {
    const values = (rows: Row[]) =>
      rows
        .filter((row) =>
          metric.cadence ? row.eligibleForCadenceComparison : row.eligibleForWorkComparison,
        )
        .map(metric.get)
        .filter((v): v is number => v !== null && Number.isFinite(v))
    const before = values(group.before)
    const after = values(group.after)
    const a = percentile(before, 0.5)
    const b = percentile(after, 0.5)
    const sufficient =
      before.length >= 3 && after.length >= 3 && group.before[0].identity.mode === 'clean'
    const change =
      a && b !== null && sufficient
        ? `${(((metric.higher ? b - a : a - b) / a) * 100).toFixed(1)}% ${sameBuild ? 'A/A difference (not gain)' : 'improvement'}`
        : 'withheld (invalid/unsupported, <3 samples, or diagnostic)'
    lines.push(
      `| ${key} | ${metric.name} | ${a?.toFixed(2) ?? '—'} | ${b?.toFixed(2) ?? '—'} | ${change} | ${before.length}/${after.length} |`,
    )
  }
  const issues = [...new Set([...group.before, ...group.after].flatMap((r) => r.issues))]
  if (issues.length) {
    lines.push(`\n${key}: ${issues.join('; ')}\n`)
  }
}
lines.push(
  '',
  'Raw JSON retains every interval, input, scheduling/ack timestamp, validation issue and outcome.',
  'Input deadline failures suppress cadence comparisons but preserve validated fixed-work completion evidence.',
  'JS heap deltas are descriptive; this benchmark does not prove leaks or total GPU/media/bitmap memory.',
  'No statistical significance claim is made from three short trials.',
)
const path = resolve(destination ?? join(afterDir, 'comparison.md'))
await writeFile(path, `${lines.join('\n')}\n`)
process.stdout.write(`Comparison written: ${path}\n`)
