import express from 'express'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { logger } from './logger.js'
import { JobManager } from './services/job-manager.js'
import { ComfyUIProvider } from './services/providers/comfyui-provider.js'
import { MockProvider } from './services/providers/mock-provider.js'
import type { GenerationProvider } from './services/providers/provider.js'
import { Store } from './store.js'
import type { BackendStatus, Job } from './types.js'
import { parseGenParams, parseInitImage } from './validate.js'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const rootDir = path.resolve(__dirname, '..')

/** 进行中(排队 + 运行)任务数上限,防止前端异常把队列灌爆。 */
const MAX_ACTIVE_JOBS = 50

interface AppConfig {
  provider: 'mock' | 'comfyui'
  comfyUrl: string
  port: number
}

/** 读取并校验运行配置:错误直接抛出快速失败,避免带病启动。 */
function loadConfig(): AppConfig {
  let file: Partial<AppConfig> = {}
  try {
    file = JSON.parse(readFileSync(path.join(rootDir, 'config.json'), 'utf8'))
  } catch {
    // 没有 config.json 时使用默认值
  }
  const provider = process.env.SWARMUI_PROVIDER ?? file.provider ?? 'mock'
  if (provider !== 'mock' && provider !== 'comfyui') {
    throw new Error(`配置错误:provider 只能是 mock 或 comfyui,当前为 "${provider}"`)
  }
  const comfyUrl = process.env.SWARMUI_COMFY_URL ?? file.comfyUrl ?? 'http://127.0.0.1:8188'
  if (provider === 'comfyui' && !/^https?:\/\//.test(comfyUrl)) {
    throw new Error(`配置错误:comfyUrl 必须以 http:// 或 https:// 开头,当前为 "${comfyUrl}"`)
  }
  const port = Number(process.env.SWARMUI_PORT ?? file.port ?? 7801)
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(`配置错误:port 必须是 1..65535 的整数,当前为 "${port}"`)
  }
  return { provider, comfyUrl, port }
}

const config = loadConfig()
const provider: GenerationProvider =
  config.provider === 'comfyui' ? new ComfyUIProvider(config.comfyUrl) : new MockProvider()
const dataDir = path.join(rootDir, 'data')
const store = new Store(path.join(dataDir, 'images'), path.join(dataDir, 'history.json'))
const jobs = new JobManager(provider, store)

const app = express()
app.disable('x-powered-by')
app.use(express.json({ limit: '12mb' }))

// 请求级访问日志(debug 级,LOG_LEVEL=debug 时输出)
app.use((req, res, next) => {
  const startAt = Date.now()
  res.on('finish', () => {
    logger.debug(`${req.method} ${req.originalUrl} -> ${res.statusCode}`, {
      ms: Date.now() - startAt,
    })
  })
  next()
})

// 后端探测结果缓存 10 秒,避免每个请求都打一次健康检查
let providerStatus: BackendStatus = { ok: true, detail: '初始化中' }
async function refreshStatus(): Promise<void> {
  try {
    providerStatus = await provider.status()
  } catch (err) {
    // provider.status 约定自行捕获异常,这里兜底避免定时器产生未处理的 Promise 拒绝
    providerStatus = { ok: false, detail: `状态探测异常:${(err as Error).message}` }
    logger.warn('后端状态探测异常', { err })
  }
}
setInterval(() => void refreshStatus(), 10_000)

app.get('/api/health', (_req, res) => {
  res.json({ ok: true, provider: provider.name, backend: providerStatus })
})

app.get('/api/models', async (_req, res) => {
  try {
    res.json({ models: await provider.listModels() })
  } catch (err) {
    res.status(502).json({ error: `获取模型列表失败:${(err as Error).message}` })
  }
})

app.get('/api/samplers', async (_req, res) => {
  try {
    res.json(await provider.listSamplerOptions())
  } catch (err) {
    res.status(502).json({ error: `获取采样器列表失败:${(err as Error).message}` })
  }
})

app.post('/api/generate', (req, res) => {
  if (jobs.listActive().length >= MAX_ACTIVE_JOBS) {
    return res.status(429).json({ error: `队列已满(${MAX_ACTIVE_JOBS} 个进行中任务),请稍后再试` })
  }
  const parsed = parseGenParams(req.body)
  if (!parsed.ok) {
    return res.status(400).json({ error: parsed.error })
  }
  const initParsed = parseInitImage((req.body as Record<string, unknown>).initImage)
  if (!initParsed.ok) {
    return res.status(400).json({ error: initParsed.error })
  }
  const job = jobs.createJob(parsed.params, initParsed.image)
  logger.info('已接受生成任务', {
    jobId: job.id,
    kind: parsed.params.kind,
    batch: parsed.params.batchCount,
  })
  return res.status(202).json({ jobId: job.id, job })
})

app.get('/api/jobs', (req, res) => {
  // 默认返回进行中任务;?all=1 返回最近任务(含已完成/失败)供任务历史面板使用
  if (req.query.all) {
    const limit = Math.min(100, Math.max(1, Number(req.query.limit) || 30))
    return res.json({ jobs: jobs.listRecent(limit) })
  }
  return res.json({ jobs: jobs.listActive() })
})

app.get('/api/jobs/:id', (req, res) => {
  const job = jobs.getJob(req.params.id)
  if (!job) {
    return res.status(404).json({ error: '任务不存在' })
  }
  return res.json({ job })
})

app.delete('/api/jobs/:id', (req, res) => {
  const job = jobs.cancel(req.params.id)
  if (!job) {
    return res.status(404).json({ error: '任务不存在或已结束' })
  }
  return res.json({ job })
})

app.get('/api/history', (req, res) => {
  const limit = Math.min(500, Math.max(1, Number(req.query.limit) || 100))
  res.json({ images: store.list(limit) })
})

app.delete('/api/images/:id', async (req, res) => {
  const removed = await store.remove(req.params.id)
  if (!removed) {
    return res.status(404).json({ error: '图片不存在' })
  }
  return res.json({ removed })
})

app.put('/api/images/:id/star', async (req, res) => {
  const starred = (req.body as { starred?: unknown } | undefined)?.starred !== false
  const record = await store.setStarred(req.params.id, starred)
  if (!record) {
    return res.status(404).json({ error: '图片不存在' })
  }
  return res.json({ image: record })
})

// SSE 实时推送:任务进度 + 图片落盘事件,连接即收到快照
app.get('/api/events', (req, res) => {
  res.set({
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    Connection: 'keep-alive',
  })
  res.flushHeaders()
  const send = (event: string, data: unknown) =>
    res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)
  send('snapshot', {
    jobs: jobs.listActive(),
    history: store.list(100),
    provider: provider.name,
    backend: providerStatus,
  })
  const onJob = (job: Job) => send('job', job)
  const onImage = (payload: { jobId: string; image: unknown }) => send('image', payload)
  jobs.on('job', onJob)
  jobs.on('image', onImage)
  logger.debug('SSE 客户端已连接')
  const heartbeat = setInterval(() => res.write(': ping\n\n'), 15_000)
  req.on('close', () => {
    clearInterval(heartbeat)
    jobs.off('job', onJob)
    jobs.off('image', onImage)
    logger.debug('SSE 客户端已断开')
  })
})

app.use('/images', express.static(path.join(dataDir, 'images'), { maxAge: '1d' }))
app.use(express.static(path.join(rootDir, 'web')))

// 404 与兜底错误处理:所有出口保持 { error } 结构,前端提示不炸
app.use((req, res) => {
  res.status(404).json({ error: `未知路径 ${req.method} ${req.path}` })
})

app.use(
  (
    err: Error & { type?: string },
    req: express.Request,
    res: express.Response,
    _next: express.NextFunction,
  ) => {
    if (err.type === 'entity.parse.failed') {
      return res.status(400).json({ error: '请求体不是合法 JSON' })
    }
    logger.error(`未处理错误 ${req.method} ${req.originalUrl}`, { err })
    return res.status(500).json({ error: '服务器内部错误' })
  },
)

await store.init()
await refreshStatus()
const server = app.listen(config.port, () => {
  logger.info(`SwarmUI-MVP 已启动: http://127.0.0.1:${config.port}`)
  logger.info(
    `provider=${provider.name}${config.provider === 'comfyui' ? ` comfyUrl=${config.comfyUrl}` : ''}`,
  )
  logger.info(`后端状态: ${providerStatus.detail}`)
})

function shutdown(signal: string): void {
  logger.info(`收到 ${signal},正在关闭`)
  server.close(() => {
    logger.info('服务已关闭')
    process.exit(0)
  })
  // 兜底:5 秒后强制退出,避免残留连接悬挂进程
  setTimeout(() => process.exit(1), 5000).unref()
}

process.on('SIGINT', () => shutdown('SIGINT'))
process.on('SIGTERM', () => shutdown('SIGTERM'))
