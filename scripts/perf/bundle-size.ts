import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdtemp, readdir, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, relative } from 'node:path'
import { brotliCompressSync, constants, gzipSync } from 'node:zlib'
import type { Metafile } from 'esbuild'

interface Size {
  raw: number
  gzip: number
  brotli: number
}
interface FileSize extends Size {
  path: string
  sha256: string
}
interface Report {
  protocol: string
  environment: { node: string; esbuild: string; lockSha256: string }
  createdAt: string
  revision: string
  dirty: boolean
  totals: { all: Size; sourceMaps: Size; runtime: Size }
  files: FileSize[]
  inputs: Record<string, Record<string, number>>
}
const protocol = 'web-bundle-v1'
const hash = (data: Buffer) => createHash('sha256').update(data).digest('hex')
const zero = (): Size => ({ raw: 0, gzip: 0, brotli: 0 })
const metrics = ['raw', 'gzip', 'brotli'] as const
function validate(report: Report): void {
  if (report.protocol !== protocol || !report.totals || !Array.isArray(report.files)) {
    throw new Error('Unsupported bundle report')
  }
}
async function readReport(path: string): Promise<Report> {
  const report = JSON.parse(await readFile(path, 'utf8')) as Report
  validate(report)
  return report
}
async function collect(directory: string, root = directory): Promise<FileSize[]> {
  const files: FileSize[] = []
  for (const entry of (await readdir(directory, { withFileTypes: true })).sort((a, b) =>
    a.name.localeCompare(b.name),
  )) {
    const path = join(directory, entry.name)
    if (entry.isDirectory()) {
      files.push(...(await collect(path, root)))
    } else if (entry.isFile()) {
      const data = await readFile(path)
      files.push({
        path: relative(root, path),
        sha256: hash(data),
        raw: data.length,
        gzip: gzipSync(data, { level: 9 }).length,
        brotli: brotliCompressSync(data, { params: { [constants.BROTLI_PARAM_QUALITY]: 11 } })
          .length,
      })
    }
  }
  return files
}
const [mode, first, second] = process.argv.slice(2)
if (mode === 'compare' && first && second) {
  const baseline = await readReport(first)
  const candidate = await readReport(second)
  if (JSON.stringify(baseline.environment) !== JSON.stringify(candidate.environment)) {
    throw new Error('Environment or lockfile differs; capture a matching baseline before comparing')
  }
  const rows = []
  for (const group of ['all', 'sourceMaps', 'runtime'] as const) {
    for (const metric of metrics) {
      const before = baseline.totals[group][metric]
      const after = candidate.totals[group][metric]
      rows.push({
        group,
        metric,
        before,
        after,
        savedBytes: before - after,
        savedPercent: before ? (((before - after) / before) * 100).toFixed(2) : 'n/a',
      })
    }
  }
  console.table(rows)
  const paths = new Set([...baseline.files, ...candidate.files].map((file) => file.path))
  console.table(
    [...paths]
      .filter((path) => !path.endsWith('.map'))
      .map((path) => {
        const before = baseline.files.find((file) => file.path === path)?.raw ?? 0
        const after = candidate.files.find((file) => file.path === path)?.raw ?? 0
        return { path, before, after, savedBytes: before - after }
      })
      .filter((row) => row.savedBytes !== 0),
  )
} else if (mode === 'capture' && first) {
  // Refuse to replace a saved baseline. Each measurement is a fresh build in a temporary directory.
  const output = await import('node:fs/promises').then((fs) => fs.open(first, 'wx'))
  const temp = await mkdtemp(join(tmpdir(), 'frayune-bundle-'))
  try {
    const outdir = join(temp, 'build')
    execFileSync(
      'node',
      ['node_modules/typescript/bin/tsc', '-p', 'tsconfig.web.json', '--outDir', outdir],
      { stdio: 'inherit' },
    )
    execFileSync('node', ['--import', 'tsx', 'scripts/build-react.ts'], {
      stdio: 'inherit',
      env: { ...process.env, BUNDLE_OUTDIR: outdir, BUNDLE_METAFILE: join(temp, 'meta.json') },
    })
    const files = await collect(outdir)
    const totals = { all: zero(), sourceMaps: zero(), runtime: zero() }
    for (const file of files) {
      for (const metric of metrics) {
        totals.all[metric] += file[metric]
        totals[file.path.endsWith('.map') ? 'sourceMaps' : 'runtime'][metric] += file[metric]
      }
    }
    const meta = JSON.parse(await readFile(join(temp, 'meta.json'), 'utf8')) as Metafile
    const inputs: Report['inputs'] = {}
    for (const [path, value] of Object.entries(meta.outputs)) {
      if (path.endsWith('.map')) {
        continue
      }
      inputs[relative(outdir, path)] = Object.fromEntries(
        Object.entries(value.inputs)
          .sort((a, b) => b[1].bytesInOutput - a[1].bytesInOutput)
          .map(([input, info]) => [input, info.bytesInOutput]),
      )
    }
    const report: Report = {
      protocol,
      createdAt: new Date().toISOString(),
      revision: execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim(),
      dirty: Boolean(execFileSync('git', ['status', '--porcelain'], { encoding: 'utf8' }).trim()),
      environment: {
        node: process.version,
        esbuild: (
          JSON.parse(await readFile('node_modules/esbuild/package.json', 'utf8')) as {
            version: string
          }
        ).version,
        lockSha256: hash(await readFile('package-lock.json')),
      },
      totals,
      files,
      inputs,
    }
    await output.writeFile(JSON.stringify(report, null, 2) + '\n')
    console.table(totals)
    console.log(`Saved ${first} (${files.length} files)`)
  } catch (error) {
    await rm(first, { force: true })
    throw error
  } finally {
    await output.close()
    await rm(temp, { recursive: true, force: true })
  }
} else {
  throw new Error(
    'Usage: bundle-size.ts capture NEW_REPORT.json | compare BASELINE.json CANDIDATE.json',
  )
}
