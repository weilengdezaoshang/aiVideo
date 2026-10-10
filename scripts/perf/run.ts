import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdtemp, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir, platform, arch, cpus, totalmem } from 'node:os'
import { join, resolve } from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'
import { build } from 'esbuild'
import {
  panTrajectory,
  summarize,
  validateRun,
  type ComparisonKey,
  type DispatchRecord,
  type ScheduledInput,
} from '../../apps/web/canvas/perf/analysis.js'
import type { BenchmarkMode, BrowserSample } from '../../apps/web/canvas/perf/protocol.js'

const options = new Map<string, string>()
for (let i = 2; i < process.argv.length; i += 2) {
  options.set(process.argv[i], process.argv[i + 1])
}
const base = options.get('--base') ?? 'http://127.0.0.1:7815'
if (!['127.0.0.1', 'localhost'].includes(new URL(base).hostname)) {
  throw new Error('Benchmark must target an isolated loopback mock server')
}
const mode = (options.get('--mode') ?? 'clean') as BenchmarkMode
if (mode !== 'clean' && mode !== 'diagnostic') {
  throw new Error('mode must be clean or diagnostic')
}
const label = options.get('--label') ?? 'baseline'
const repeats = Number(options.get('--repeats') ?? 3)
const dpr = Number(options.get('--dpr') ?? 2)
if (!Number.isInteger(repeats) || repeats < 1 || !Number.isFinite(dpr) || dpr <= 0) {
  throw new Error('Invalid repeats/dpr')
}
const output = resolve(
  options.get('--out') ?? join(tmpdir(), `aivideo-perf-${label}-${Date.now()}`),
)
await mkdir(output, { recursive: true })
const sha = (value: string | Uint8Array) => createHash('sha256').update(value).digest('hex')
const profile = await mkdtemp(join(tmpdir(), 'canvas-benchmark-chrome-'))
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

class CDP {
  private socket: WebSocket
  private sequence = 0
  private pending = new Map<number, { resolve(value: unknown): void; reject(error: Error): void }>()
  constructor(url: string) {
    this.socket = new WebSocket(url)
    this.socket.addEventListener('message', (event) => {
      const message = JSON.parse(String(event.data)) as {
        id?: number
        result?: unknown
        error?: unknown
      }
      if (message.id === undefined) {
        return
      }
      const pending = this.pending.get(message.id)
      this.pending.delete(message.id)
      if (message.error) {
        pending?.reject(new Error(JSON.stringify(message.error)))
      } else {
        pending?.resolve(message.result)
      }
    })
    this.socket.addEventListener('close', () => {
      for (const pending of this.pending.values()) {
        pending.reject(new Error('CDP connection closed'))
      }
      this.pending.clear()
    })
  }
  async ready() {
    await new Promise<void>((resolve, reject) => {
      this.socket.addEventListener('open', () => resolve(), { once: true })
      this.socket.addEventListener('error', () => reject(new Error('CDP failed')), { once: true })
    })
  }
  async send<T = Record<string, unknown>>(
    method: string,
    params: Record<string, unknown> = {},
  ): Promise<T> {
    const id = ++this.sequence
    const result = new Promise<unknown>((resolve, reject) =>
      this.pending.set(id, { resolve, reject }),
    )
    const timer = setTimeout(() => {
      this.pending.get(id)?.reject(new Error(`CDP timeout: ${method}`))
      this.pending.delete(id)
    }, 60000)
    this.socket.send(JSON.stringify({ id, method, params }))
    try {
      // CDP JSON is the sole untyped transport boundary; callers supply command result types.
      return (await result) as T
    } finally {
      clearTimeout(timer)
    }
  }
  async evaluate<T>(expression: string): Promise<T> {
    const result = await this.send<{
      result: { value: T }
      exceptionDetails?: unknown
    }>('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })
    if (result.exceptionDetails) {
      throw new Error(JSON.stringify(result.exceptionDetails))
    }
    return result.result.value
  }
  close() {
    this.socket.close()
  }
}

async function dispatch(cdp: CDP, event: ScheduledInput) {
  if (['move', 'down', 'up'].includes(event.kind)) {
    await cdp.send('Input.dispatchMouseEvent', {
      type:
        event.kind === 'move'
          ? 'mouseMoved'
          : event.kind === 'down'
            ? 'mousePressed'
            : 'mouseReleased',
      x: event.x,
      y: event.y,
      button: 'left',
      buttons: event.kind === 'up' ? 0 : 1,
      ...(event.kind === 'move' ? {} : { clickCount: 1 }),
    })
  } else {
    const space = event.kind.startsWith('space')
    await cdp.send('Input.dispatchKeyEvent', {
      type: event.kind.endsWith('down') ? 'keyDown' : 'keyUp',
      key: space ? ' ' : 'a',
      code: space ? 'Space' : 'KeyA',
      windowsVirtualKeyCode: space ? 32 : 65,
      modifiers: space ? 0 : 2,
    })
  }
}

try {
  let port = 0
  for (let i = 0; i < 100; i++) {
    if (launchError) {
      throw launchError
    }
    try {
      port = Number((await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0])
      if (port) {
        break
      }
    } catch {
      // Discover only the debugging endpoint created by our unique profile.
    }
    await sleep(100)
  }
  if (!port) {
    throw new Error('Isolated Chrome did not publish its debugging port')
  }
  let target: { webSocketDebuggerUrl: string; id: string } | undefined
  for (let i = 0; i < 100; i++) {
    if (launchError) {
      throw launchError
    }
    try {
      const targets = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()) as Array<{
        type: string
        webSocketDebuggerUrl: string
        id: string
      }>
      target = targets.find((t) => t.type === 'page')
      if (target) {
        break
      }
    } catch {
      /* Wait for our isolated browser process to start. */
    }
    await sleep(100)
  }
  if (!target) {
    throw new Error('Isolated Chrome not available')
  }
  const cdp = new CDP(target.webSocketDebuggerUrl)
  await cdp.ready()
  try {
    const browser = await cdp.send<{ product: string; revision: string; userAgent: string }>(
      'Browser.getVersion',
    )
    await cdp.send('Page.enable')
    await cdp.send('Runtime.enable')
    await cdp.send('Emulation.setFocusEmulationEnabled', { enabled: true })
    await cdp.send('Network.enable')
    await cdp.send('Emulation.setDeviceMetricsOverride', {
      width: 1440,
      height: 900,
      deviceScaleFactor: dpr,
      mobile: false,
    })
    const bundle = await build({
      entryPoints: ['apps/web/canvas/perf/browser-collector.ts'],
      bundle: true,
      format: 'iife',
      platform: 'browser',
      write: false,
    })
    await cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: bundle.outputFiles[0].text })
    const manifest = (await (await fetch(`${base}/benchmark/scenes`)).json()) as {
      protocol: string
      scenes: Array<{ name: string; id: string; count: number; hash: string }>
    }
    if (manifest.protocol !== 'canvas-native-v3') {
      throw new Error('Wrong fixture server/protocol')
    }
    const hashes: Record<string, string> = {}
    async function hashTree(dir: string) {
      for (const entry of await readdir(dir, { withFileTypes: true })) {
        const path = join(dir, entry.name)
        if (entry.isDirectory()) {
          await hashTree(path)
        } else if (/\.(ts|tsx|js|css|html)$/.test(path)) {
          hashes[path] = sha(await readFile(path))
        }
      }
    }
    await hashTree('apps/web/canvas')
    await hashTree('apps/web/canvas')
    await hashTree('.web-build/canvas')
    await writeFile(
      join(output, 'environment.json'),
      JSON.stringify(
        {
          protocol: manifest.protocol,
          label,
          mode,
          browser,
          headless: true,
          platform: `${platform()}-${arch()}`,
          cpu: cpus()[0]?.model,
          ramBytes: totalmem(),
          node: process.version,
          sourceHashes: hashes,
          collectorHash: sha(bundle.outputFiles[0].text),
          fixture: manifest,
          dpr,
          viewport: { width: 1440, height: 900 },
          cachePolicy: 'fresh-page-cache-cleared-assets-settled',
        },
        null,
        2,
      ),
    )
    const rows: unknown[] = []
    for (const scene of manifest.scenes) {
      if (options.has('--scene') && scene.name !== options.get('--scene')) {
        continue
      }
      for (const action of ['idle', 'pan', 'pan-work', 'select-all'] as const) {
        if (options.has('--action') && action !== options.get('--action')) {
          continue
        }
        for (let trial = 0; trial < repeats; trial++) {
          process.stdout.write(`Preparing ${scene.name}/${action}/${trial}\n`)
          await cdp.send('Network.clearBrowserCache')
          await cdp.send('Storage.clearDataForOrigin', {
            origin: new URL(base).origin,
            storageTypes: 'local_storage',
          })
          await cdp.send('Page.navigate', {
            url: `${base}/benchmark?doc=${scene.id}&benchmark=1&mode=${mode}`,
          })
          let ready = false
          for (let i = 0; i < 600; i++) {
            try {
              ready = await cdp.evaluate<boolean>('Boolean(window.__canvasCollector?.ready())')
            } catch {
              /* navigation may temporarily destroy the execution context */
            }
            if (ready) {
              break
            }
            await sleep(100)
          }
          if (!ready) {
            throw new Error(`Canvas not ready: ${scene.name}`)
          }
          await cdp.send('Page.bringToFront')
          process.stdout.write(`Canvas ready; sampling ${scene.name}/${action}/${trial}\n`)
          await sleep(2000)
          const rect = await cdp.evaluate<{ x: number; y: number; width: number; height: number }>(
            'document.querySelector(".konvajs-content").getBoundingClientRect().toJSON()',
          )
          const x = Math.round(rect.x + rect.width / 2)
          const y = Math.round(rect.y + rect.height / 2)
          // Focus without changing selection; exclude this setup event from the sample.
          await cdp.evaluate<void>(
            'document.querySelector(".konvajs-content").closest("[tabindex]")?.focus()',
          )
          const trajectory: ScheduledInput[] =
            action === 'pan' || action === 'pan-work'
              ? panTrajectory(x, y, action === 'pan-work' ? 0 : 3000)
              : action === 'select-all'
                ? [
                    { atMs: 0, kind: 'select-all-down', x, y },
                    { atMs: 50, kind: 'select-all-up', x, y },
                  ]
                : []
          if (mode === 'diagnostic') {
            await cdp.send('Profiler.enable')
            await cdp.send('Profiler.start')
          }
          await cdp.evaluate<void>('window.__canvasCollector.start()')
          const began = performance.now()
          const records: DispatchRecord[] = []
          const acknowledgements: Promise<void>[] = []
          const transportErrors: string[] = []
          let shiftMs = 0
          for (const event of trajectory) {
            const remaining = began + event.atMs + shiftMs - performance.now()
            if (remaining > 0) {
              await sleep(remaining)
            }
            const sentMs = performance.now() - began
            const lateness = sentMs - event.atMs - shiftMs
            // Never burst all overdue events into a busy renderer.
            if (lateness > 100) {
              shiftMs += lateness
            }
            if (options.get('--verbose') === 'true') {
              process.stdout.write(
                `send ${event.kind} planned=${event.atMs} actual=${sentMs.toFixed(1)}\n`,
              )
            }
            const record: DispatchRecord = {
              kind: event.kind,
              atMs: event.atMs,
              sentMs,
              ackMs: -1,
              x: event.x,
              y: event.y,
            }
            records.push(record)
            // CDP transport acknowledgements are not rendering completion. Some Chrome
            // versions delay mouseMoved ACKs by seconds; never let that change input cadence.
            const acknowledgement = dispatch(cdp, event)
              .then(() => {
                record.ackMs = performance.now() - began
              })
              .catch((error: unknown) => {
                transportErrors.push(error instanceof Error ? error.message : String(error))
              })
            acknowledgements.push(acknowledgement)
            if (action === 'pan-work') {
              await acknowledgement
              if (event.kind === 'move') {
                const expected = records.filter((record) => record.kind === 'move').length
                const received = await cdp.evaluate<number>(
                  'window.__canvasCollector.receivedMoves()',
                )
                if (received !== expected) {
                  transportErrors.push('fixed-work move was not individually observed')
                }
              }
            }
          }
          // Completion proxy, not actual GPU presentation. Wait for two rendering opportunities.
          await cdp.evaluate<void>(
            'new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r())))',
          )
          const completionMs = performance.now() - began
          if (action === 'idle') {
            await sleep(3200)
          } else {
            await sleep(300)
          }
          const sample = await cdp.evaluate<BrowserSample>('window.__canvasCollector.stop()')
          const cpu = mode === 'diagnostic' ? await cdp.send('Profiler.stop') : null
          await Promise.all(acknowledgements)
          const issues = [...validateRun(sample, records, action, scene.count), ...transportErrors]
          const identity: ComparisonKey = {
            protocol: manifest.protocol,
            sceneHash: scene.hash,
            sceneName: scene.name,
            action,
            mode,
            browser: browser.product,
            width: 1440,
            height: 900,
            dpr,
            platform: `${platform()}-${arch()}`,
            cachePolicy: 'fresh-page-cache-cleared-assets-settled',
            trajectoryHash: sha(JSON.stringify(trajectory)),
            initialStateHash: sha(JSON.stringify(sample.initial)),
          }
          const stem = `${scene.name}-${action}-${trial}`
          const result = {
            label,
            trial,
            identity,
            timestamp: new Date().toISOString(),
            completionProxyMs: action === 'idle' ? null : completionMs,
            dispatch: records,
            sample,
            summary: summarize(sample),
            issues,
            eligibleForCadenceComparison: issues.length === 0 && action !== 'pan-work',
            delivery: {
              offeredMoves: records.filter((event) => event.kind === 'move').length,
              observedMoves: sample.inputs.filter((event) => event.type === 'mousemove').length,
            },
            eligibleForWorkComparison:
              (action === 'pan-work' || action === 'select-all') &&
              issues.every((issue) => issue.startsWith('input schedule missed')),
          }
          await writeFile(join(output, `${stem}.json`), JSON.stringify(result, null, 2))
          if (mode === 'diagnostic') {
            await writeFile(join(output, `${stem}.cpuprofile`), JSON.stringify(cpu?.profile))
          }
          const screenshot = await cdp.send<{ data: string }>('Page.captureScreenshot', {
            format: 'png',
          })
          await writeFile(join(output, `${stem}.png`), Buffer.from(screenshot.data, 'base64'))
          rows.push({
            file: `${stem}.json`,
            ...result.summary,
            completionProxyMs: result.completionProxyMs,
            issues,
          })
          process.stdout.write(
            `${stem}: ${result.summary.rafFps.toFixed(1)} rAF FPS; ${issues.join('; ') || 'validated'}\n`,
          )
        }
      }
    }
    if (!rows.length) {
      throw new Error('No matching scenes/actions')
    }
    await writeFile(join(output, 'summary.json'), JSON.stringify(rows, null, 2))
    process.stdout.write(`Raw benchmark evidence: ${output}\n`)
  } finally {
    cdp.close()
  }
} finally {
  chrome.kill('SIGTERM')
  await sleep(500)
  await rm(profile, { recursive: true, force: true }).catch(() => {})
}
