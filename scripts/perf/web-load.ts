import { spawn, execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdtemp, mkdir, readFile, writeFile, rm, readdir } from 'node:fs/promises'
import { tmpdir, cpus, totalmem, platform, arch } from 'node:os'
import { join, resolve } from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'
import { build } from 'esbuild'
import { WebCDP } from './web-cdp.js'
import type { WebSample } from './web-probe.js'

interface RequestRow {
  id: string
  url: string
  type: string
  status?: number
  mime?: string
  cache?: boolean
  encoding?: string
  cacheControl?: string
  receivedBytes: number
  finished?: boolean
  failed?: string
}
export interface Run {
  page: string
  cache: string
  trial: number
  sample: WebSample
  requests: RequestRow[]
  issues: string[]
  networkBytes: number
  jsNetworkBytes: number
  jsDecodedBytes: number
}
const options = new Map<string, string>()
for (let i = 2; i < process.argv.length; i += 2) {
  options.set(process.argv[i], process.argv[i + 1])
}
const base = options.get('--base') ?? 'http://127.0.0.1:7815'
if (!['127.0.0.1', 'localhost'].includes(new URL(base).hostname)) {
  throw new Error('Use isolated loopback fixture server')
}
const repeats = Number(options.get('--repeats') ?? 3)
if (!Number.isInteger(repeats) || repeats < 1) {
  throw new Error('Invalid repeats')
}
const output = resolve(options.get('--out') ?? join(tmpdir(), `web-baseline-${Date.now()}`))
await mkdir(output) // Refuse to overwrite a saved result directory.
const fixture = (await (await fetch(`${base}/benchmark/scenes`)).json()) as {
  protocol: string
  scenes: { name: string; id: string; count: number; hash: string }[]
}
if (fixture.protocol !== 'canvas-native-v3') {
  throw new Error('Wrong fixture server')
}
const scene = fixture.scenes.find((item) => item.count === 500)
if (!scene) {
  throw new Error('Missing fixed scene')
}
const profile = await mkdtemp(join(tmpdir(), 'web-load-chrome-'))
const chromePath =
  options.get('--chrome') ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const chrome = spawn(
  chromePath,
  [
    '--remote-debugging-port=0',
    '--remote-debugging-address=127.0.0.1',
    `--user-data-dir=${profile}`,
    '--headless=new',
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-background-timer-throttling',
    '--disable-renderer-backgrounding',
    'about:blank',
  ],
  { stdio: 'ignore' },
)
let launchError: Error | undefined
chrome.on('error', (error) => {
  launchError = error
})
let cdp: WebCDP | undefined
const settings = {
  width: 1440,
  height: 900,
  dpr: 1,
  cpuSlowdown: 1,
  latencyMs: 40,
  downloadBytesPerSecond: 1250000,
  uploadBytesPerSecond: 250000,
  headless: true,
  device: 'desktop Chrome; headless laboratory run',
  settleAfterReadyMs: 5000,
}
const hash = (data: string | Buffer) => createHash('sha256').update(data).digest('hex')
try {
  let port = 0
  for (let i = 0; i < 100; i++) {
    if (launchError) {
      throw launchError
    }
    try {
      port = Number((await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0])
    } catch {
      /* browser starting */
    }
    if (port) {
      break
    }
    await sleep(100)
  }
  if (!port) {
    throw new Error('Chrome did not publish debugging port')
  }
  const targets = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()) as {
    type: string
    webSocketDebuggerUrl: string
  }[]
  const target = targets.find((item) => item.type === 'page')
  if (!target) {
    throw new Error('No page target')
  }
  cdp = new WebCDP(target.webSocketDebuggerUrl)
  await cdp.ready()
  const browser = await cdp.send<{ product: string; revision: string; userAgent: string }>(
    'Browser.getVersion',
  )
  const probe = await build({
    entryPoints: ['scripts/perf/web-probe.ts'],
    bundle: true,
    format: 'iife',
    platform: 'browser',
    write: false,
  })
  await cdp.send('Page.enable')
  await cdp.send('Runtime.enable')
  await cdp.send('Network.enable')
  await cdp.send('Emulation.setFocusEmulationEnabled', { enabled: true })
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: settings.width,
    height: settings.height,
    deviceScaleFactor: settings.dpr,
    mobile: false,
  })
  await cdp.send('Emulation.setCPUThrottlingRate', { rate: settings.cpuSlowdown })
  await cdp.send('Network.emulateNetworkConditions', {
    offline: false,
    latency: settings.latencyMs,
    downloadThroughput: settings.downloadBytesPerSecond,
    uploadThroughput: settings.uploadBytesPerSecond,
    connectionType: 'cellular4g',
  })
  await cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: probe.outputFiles[0].text })
  const hashes: Record<string, string> = {}
  async function hashTree(dir: string) {
    for (const entry of await readdir(dir, { withFileTypes: true })) {
      const path = join(dir, entry.name)
      if (entry.isDirectory()) {
        await hashTree(path)
      } else if (/\.(js|css|html|woff2?|webp|png|jpg|svg)$/.test(path)) {
        hashes[path] = hash(await readFile(path))
      }
    }
  }
  await hashTree('.web-build')
  await hashTree('apps/web')
  await writeFile(
    join(output, 'environment.json'),
    JSON.stringify(
      {
        protocol: 'web-load-v1',
        createdAt: new Date().toISOString(),
        settings,
        browser,
        node: process.version,
        platform: `${platform()}-${arch()}`,
        cpu: cpus()[0]?.model,
        ram: totalmem(),
        revision: execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim(),
        dirty: true,
        collectorHash: hash(probe.outputFiles[0].text),
        hashes,
        fixture,
      },
      null,
      2,
    ),
  )
  let rows = new Map<string, RequestRow>()
  let errors: string[] = []
  cdp.onEvent = (method, params) => {
    const id = String(params.requestId ?? '')
    if (method === 'Network.requestWillBeSent') {
      const request = params.request as { url: string }
      rows.set(id, { id, url: request.url, type: String(params.type), receivedBytes: 0 })
    }
    const row = rows.get(id)
    if (row && method === 'Network.responseReceived') {
      const response = params.response as {
        status: number
        mimeType: string
        fromDiskCache?: boolean
        fromServiceWorker?: boolean
        headers: Record<string, string>
      }
      const headers = Object.fromEntries(
        Object.entries(response.headers).map(([key, value]) => [key.toLowerCase(), value]),
      )
      Object.assign(row, {
        status: response.status,
        mime: response.mimeType,
        cache: row.cache || response.fromDiskCache || response.fromServiceWorker,
        encoding: headers['content-encoding'] ?? 'identity',
        cacheControl: headers['cache-control'] ?? '',
      })
    }
    if (row && method === 'Network.requestServedFromCache') {
      row.cache = true
    }
    if (row && method === 'Network.dataReceived') {
      row.receivedBytes += Number(params.encodedDataLength ?? 0)
    }
    if (row && method === 'Network.loadingFinished') {
      row.receivedBytes = row.cache ? 0 : Number(params.encodedDataLength)
      row.finished = true
    }
    if (row && method === 'Network.loadingFailed') {
      row.failed = String(params.errorText)
      row.finished = true
    }
    if (method === 'Runtime.exceptionThrown') {
      errors.push(JSON.stringify(params.exceptionDetails))
    }
  }
  const runs: Run[] = []
  const pages = [
    { name: 'landing', path: '/' },
    { name: 'workspace', path: '/workspace' },
    { name: 'canvas-text-500', path: `/canvas?doc=${scene.id}&benchmark=1&mode=clean` },
  ]
  for (const page of pages) {
    for (let trial = 1; trial <= repeats; trial++) {
      for (const cache of ['cold', 'warm']) {
        await cdp.send('Page.navigate', { url: 'about:blank' })
        await sleep(300)
        if (cache === 'cold') {
          await cdp.send('Network.clearBrowserCache')
          await cdp.send('Storage.clearDataForOrigin', {
            origin: new URL(base).origin,
            storageTypes: 'local_storage,cookies,service_workers,cache_storage',
          })
        } else {
          // Warm HTTP cache only: keep the same application state as the cold run.
          await cdp.send('Storage.clearDataForOrigin', {
            origin: new URL(base).origin,
            storageTypes: 'local_storage',
          })
        }
        await cdp.send('Network.setCacheDisabled', { cacheDisabled: false })
        rows = new Map()
        errors = []
        console.log(`${page.name} ${cache} ${trial}/${repeats}`)
        await cdp.send('Page.navigate', { url: `${base}${page.path}` })
        let sample: WebSample | undefined
        const start = Date.now()
        while (Date.now() - start < 90000) {
          await sleep(250)
          try {
            sample = await cdp.evaluate<WebSample | undefined>('window.__webLoadProbe?.snapshot()')
          } catch {
            /* navigation replacing context */
          }
          if (
            sample?.domReadyProxyMs !== null &&
            sample?.domReadyProxyMs !== undefined &&
            sample.observedUntilMs > sample.domReadyProxyMs + settings.settleAfterReadyMs &&
            sample.fontReadyMs !== null &&
            ![...rows.values()].some(
              (row) => !row.finished && !['EventSource', 'WebSocket'].includes(row.type),
            )
          ) {
            break
          }
        }
        if (!sample) {
          throw new Error(`No probe data: ${page.name}`)
        }
        const requests = [...rows.values()]
        const issues = [
          ...errors,
          ...requests
            .filter((row) => row.type !== 'EventSource' && (row.failed || (row.status ?? 0) >= 400))
            .map((row) => `${row.url}: ${row.failed ?? row.status}`),
        ]
        if (sample.domReadyProxyMs === null) {
          issues.push('DOM readiness timed out')
        }
        if (sample.hidden) {
          issues.push('Page hidden during measurement')
        }
        if (
          requests.some((row) => !row.finished && !['EventSource', 'WebSocket'].includes(row.type))
        ) {
          issues.push('Resource loading timed out')
        }
        const regular = requests.filter((row) => !['EventSource', 'WebSocket'].includes(row.type))
        const run: Run = {
          page: page.name,
          cache,
          trial,
          sample,
          requests,
          issues,
          networkBytes: regular.reduce((sum, row) => sum + row.receivedBytes, 0),
          jsNetworkBytes: regular
            .filter((row) => row.type === 'Script')
            .reduce((sum, row) => sum + row.receivedBytes, 0),
          jsDecodedBytes: sample.resources
            .filter((row) => row.type === 'script')
            .reduce((sum, row) => sum + row.decoded, 0),
        }
        runs.push(run)
        await writeFile(
          join(output, `${page.name}-${cache}-${trial}.json`),
          JSON.stringify(run, null, 2),
        )
        if (trial === 1) {
          const screenshot = await cdp.send<{ data: string }>('Page.captureScreenshot', {
            format: 'png',
          })
          await writeFile(
            join(output, `${page.name}-${cache}.png`),
            Buffer.from(screenshot.data, 'base64'),
          )
        }
        console.log(
          `  ready=${sample.domReadyProxyMs?.toFixed(0)}ms LCP=${sample.lcpObservedMs?.toFixed(0)}ms bytes=${run.networkBytes} issues=${issues.length}`,
        )
      }
    }
  }
  const median = (values: number[]) => {
    const sorted = [...values].sort((a, b) => a - b)
    return sorted.length ? sorted[Math.floor(sorted.length / 2)] : null
  }
  const lines = [
    '# 网页桌面端加载基线',
    '',
    '桌面 Chrome headless；1440×900，DPR 1，CPU 不降速，下载 10 Mbps，上传 2 Mbps，延迟 40ms。',
    '',
    'LCP/CLS 为截至采样结束的观察值；DOM 就绪是结构代理，不是实际交互完成。画布使用固定 500 个文字对象。',
    '',
    '| 页面 | 缓存 | 有效轮数 | 下载 KB | JS 下载 KB | FCP ms | LCP 观察值 ms | DOM 就绪代理 ms | CLS 观察值 |',
    '|---|---|---:|---:|---:|---:|---:|---:|---:|',
  ]
  for (const page of pages) {
    for (const cache of ['cold', 'warm']) {
      const valid = runs.filter(
        (run) => run.page === page.name && run.cache === cache && !run.issues.length,
      )
      const m = (fn: (run: Run) => number | null) =>
        median(valid.map(fn).filter((value): value is number => value !== null))
      lines.push(
        `| ${page.name} | ${cache} | ${valid.length}/${repeats} | ${((m((run) => run.networkBytes) ?? 0) / 1024).toFixed(1)} | ${((m((run) => run.jsNetworkBytes) ?? 0) / 1024).toFixed(1)} | ${m((run) => run.sample.fcpMs)?.toFixed(0) ?? '—'} | ${m((run) => run.sample.lcpObservedMs)?.toFixed(0) ?? '—'} | ${m((run) => run.sample.domReadyProxyMs)?.toFixed(0) ?? '—'} | ${m((run) => run.sample.clsObserved)?.toFixed(4) ?? '—'} |`,
      )
    }
  }
  lines.push(
    '',
    '每项为有效轮次的中位数。3 轮为初步基线，不证明小幅变化显著。原始 JSON 含请求、缓存命中、压缩、长任务、字体及导航时序。',
  )
  await writeFile(join(output, 'summary.md'), lines.join('\n') + '\n')
  console.log(lines.join('\n'))
} finally {
  cdp?.close()
  chrome.kill('SIGTERM')
  await Promise.race([
    new Promise<void>((resolve) => chrome.once('exit', () => resolve())),
    sleep(3000),
  ])
  if (chrome.exitCode === null) {
    chrome.kill('SIGKILL')
    await sleep(500)
  }
  await rm(profile, { recursive: true, force: true })
}
