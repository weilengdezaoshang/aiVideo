import test from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { spawn } from 'node:child_process'
import type { ChildProcess } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { once } from 'node:events'
import { cp, mkdtemp, rm, symlink } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import type { AddressInfo } from 'node:net'
import type { Job } from '../src/types.js'

test(
  'HTTP 全链路:Mock 切百炼、修改云模型热生效、生成任务与图片下载（假 Key/本地上游）',
  { timeout: 20000 },
  async (t) => {
    const dir = await mkdtemp(path.join(tmpdir(), 'aivideo-http-'))
    const resources: { child?: ChildProcess } = {}
    t.after(async () => {
      const child = resources.child
      if (child && child.exitCode === null && child.signalCode === null) {
        const exited = once(child, 'exit')
        child.kill('SIGTERM')
        await exited
      }
      await rm(dir, { recursive: true, force: true })
    })
    const requests: Array<{ model: string; authorization?: string }> = []
    const png = Buffer.from(
      'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=',
      'base64',
    )
    const upstream = createServer(async (req, res) => {
      if (req.url === '/image.png') {
        assert.equal(req.headers.authorization, undefined)
        res.writeHead(200, { 'Content-Type': 'image/png' }).end(png)
        return
      }
      assert.equal(req.url, '/api/v1/services/aigc/multimodal-generation/generation')
      let raw = ''
      for await (const chunk of req) {
        raw += chunk
      }
      const body = JSON.parse(raw)
      requests.push({ model: body.model, authorization: req.headers.authorization })
      res.setHeader('Content-Type', 'application/json')
      res.end(
        JSON.stringify({
          output: {
            choices: [
              {
                message: {
                  content: [
                    {
                      image: `http://127.0.0.1:${(upstream.address() as AddressInfo).port}/image.png`,
                    },
                  ],
                },
              },
            ],
          },
        }),
      )
    })
    upstream.listen(0, '127.0.0.1')
    await once(upstream, 'listening')
    t.after(() => {
      upstream.closeAllConnections()
      upstream.close()
    })
    const probe = createServer()
    probe.listen(0, '127.0.0.1')
    await once(probe, 'listening')
    const port = (probe.address() as AddressInfo).port
    await new Promise<void>((resolve) => probe.close(() => resolve()))
    const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
    await cp(path.join(root, 'src'), path.join(dir, 'src'), { recursive: true })
    await cp(path.join(root, 'package.json'), path.join(dir, 'package.json'))
    await symlink(path.join(root, 'node_modules'), path.join(dir, 'node_modules'))
    await symlink(path.join(root, 'web'), path.join(dir, 'web'))
    const child = spawn(process.execPath, ['--import', 'tsx', 'src/server.ts'], {
      cwd: dir,
      env: { PATH: process.env.PATH, SWARMUI_PORT: String(port), LOG_LEVEL: 'error' },
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    resources.child = child
    let output = ''
    child.stdout!.on('data', (data) => {
      output += data
    })
    child.stderr!.on('data', (data) => {
      output += data
    })
    const base = `http://127.0.0.1:${port}`
    let ready = false
    for (let i = 0; i < 100; i++) {
      if (child.exitCode !== null) {
        assert.fail(output)
      }
      try {
        ready = (await fetch(`${base}/api/models`)).ok
      } catch {
        /* 启动中 */
      }
      if (ready) {
        break
      }
      await delay(50)
    }
    assert.ok(ready, output)
    const post = async (url: string, body: unknown) => {
      const res = await fetch(`${base}${url}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      assert.ok(res.ok, `HTTP ${res.status}`)
      return res.json()
    }
    await post('/api/config', {
      provider: 'cloud',
      cloudVendor: 'aliyun',
      cloudModel: 'qwen-image-2.0',
      cloudBaseUrl: `http://127.0.0.1:${(upstream.address() as AddressInfo).port}/api/v1`,
      imageApiKey: 'sk-local-fixture',
    })
    const changed = await post('/api/config', {
      cloudModel: 'qwen-image-2.0-pro',
      imageApiKey: 'sk-local-updated',
    })
    assert.equal(changed.providerSwitched, true)
    assert.ok(!JSON.stringify(changed).includes('sk-local-updated'))
    const check = await post('/api/config/test', {})
    assert.match(check.detail, /未验证鉴权/)
    assert.equal(requests.length, 0)
    const models = await (await fetch(`${base}/api/models`)).json()
    assert.equal(models.models[0].id, 'qwen-image-2.0-pro')
    const submitted = await post('/api/generate', {
      prompt: '本地协议测试',
      model: 'qwen-image-2.0-pro',
      width: 1024,
      height: 1024,
      batchCount: 1,
    })
    let job: Job | undefined
    for (let i = 0; i < 100; i++) {
      job = (await (await fetch(`${base}/api/jobs/${submitted.jobId}`)).json()).job
      if (job?.status === 'completed' || job?.status === 'failed') {
        break
      }
      await delay(50)
    }
    assert.equal(job?.status, 'completed', job?.error)
    assert.deepEqual(requests, [
      { model: 'qwen-image-2.0-pro', authorization: 'Bearer sk-local-updated' },
    ])
    const image = await fetch(`${base}${job!.images[0].url}`)
    assert.deepEqual(Buffer.from(await image.arrayBuffer()), png)
    await post('/api/config', { cloudModel: 'qwen-image-2.0' })
    const edited = await post('/api/generate', {
      prompt: '一只白色的猫',
      model: 'qwen-image-2.0',
      width: 1024,
      height: 1024,
      batchCount: 1,
      initImage: `data:image/png;base64,${png.toString('base64')}`,
    })
    assert.equal(edited.job.params.model, 'qwen-image-edit-plus')
    for (let i = 0; i < 100; i++) {
      job = (await (await fetch(`${base}/api/jobs/${edited.jobId}`)).json()).job
      if (job?.status === 'completed' || job?.status === 'failed') {
        break
      }
      await delay(50)
    }
    assert.equal(job?.status, 'completed', job?.error)
    assert.equal(requests.at(-1)?.model, 'qwen-image-edit-plus')
    assert.equal(job?.images[0].params.model, 'qwen-image-edit-plus')
    const html = await (await fetch(`${base}/canvas/`)).text()
    assert.match(html, /value="aliyun"/)
  },
)
