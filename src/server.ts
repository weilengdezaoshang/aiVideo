import express from 'express'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { applyConfigPatch, loadConfig, publicConfig, saveConfigPatch } from './config.js'
import type { AppConfig, ConfigPatch } from './config.js'
import { logger } from './logger.js'
import { AssetStore } from './assets.js'
import { DocumentStore } from './documents.js'
import { JobManager } from './services/job-manager.js'
import { CloudImagesProvider } from './services/providers/cloud-provider.js'
import { ComfyUIProvider } from './services/providers/comfyui-provider.js'
import { MockProvider } from './services/providers/mock-provider.js'
import type { GenerationProvider } from './services/providers/provider.js'
import { Store } from './store.js'
import { classifyError, estimateCostCny, promptHash, TraceStore } from './traces.js'
import type { BackendStatus, Job } from './types.js'
import { parseGenParams, parseInitImage, parseMaskImage } from './validate.js'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const rootDir = path.resolve(__dirname, '..')

/** 进行中(排队 + 运行)任务数上限,防止前端异常把队列灌爆。 */
const MAX_ACTIVE_JOBS = 50

/** 由配置构建生成后端(provider 热切换时重建实例)。 */
function buildProvider(config: AppConfig): GenerationProvider {
  if (config.provider === 'cloud') {
    return new CloudImagesProvider({
      vendor: config.cloudVendor,
      baseUrl: config.cloudBaseUrl,
      model: config.cloudModel,
      apiKey: config.imageApiKey,
      imageTimeoutMs: config.imageTimeoutMin * 60_000,
    })
  }
  return config.provider === 'comfyui'
    ? new ComfyUIProvider(config.comfyUrl, {
        imageTimeoutMs: config.imageTimeoutMin * 60_000,
        videoTimeoutMs: config.videoTimeoutMin * 60_000,
        videoModel: config.videoModel,
        videoBackend: config.videoBackend,
        wanClip: config.wanClip,
        wanVae: config.wanVae,
        wanHighNoiseUnet: config.wanHighNoiseUnet,
        wanLowNoiseUnet: config.wanLowNoiseUnet,
      })
    : new MockProvider()
}

let config = loadConfig(rootDir)
let provider = buildProvider(config)
const dataDir = path.join(rootDir, 'data')
const store = new Store(path.join(dataDir, 'images'), path.join(dataDir, 'history.json'))
const assets = new AssetStore(path.join(dataDir, 'assets'))
const documents = new DocumentStore(path.join(dataDir, 'documents'))
const jobs = new JobManager(provider, store, path.join(dataDir, 'jobs.json'))
// 生成链路追踪(PRD 目标:质量可评测、行为可追踪)
const traces = new TraceStore(path.join(dataDir, 'traces.jsonl'))

/** 终态任务 → 追踪记录;错误分类 + 费用估算,排队/生成分离计时 */
jobs.on('job', (job: Job) => {
  if (job.status !== 'completed' && job.status !== 'failed') {
    return
  }
  const finished = Date.parse(job.finishedAt ?? '') || Date.now()
  const started = Date.parse(job.startedAt ?? '') || finished
  const created = Date.parse(job.createdAt) || started
  const status =
    job.status === 'completed'
      ? 'completed'
      : /已取消/.test(job.error ?? '')
        ? 'cancelled'
        : 'failed'
  void traces.append({
    ts: new Date().toISOString(),
    kind: 'trace',
    jobId: job.id,
    clientRef: job.clientRef,
    provider: provider.name,
    vendor: provider.name === 'cloud' ? config.cloudVendor : undefined,
    model: job.params.model,
    origin: 'user',
    promptHash: promptHash(job.params.prompt),
    promptChars: job.params.prompt.length,
    seed: job.params.seed,
    width: job.params.width,
    height: job.params.height,
    kind_of_media: job.params.kind,
    status,
    queueMs: Math.max(0, started - created),
    latencyMs: Math.max(0, finished - started),
    imageId: job.images[0]?.id,
    costCny: status === 'completed' ? estimateCostCny(job.params.model) : undefined,
    errorCategory: status === 'failed' ? classifyError(job.error ?? '') : undefined,
    errorMessage: status === 'failed' ? job.error : undefined,
  })
})

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

// 指标聚合(可评测):成功率 / 延迟分位 / 费用 / 错误分布 / 保留率
app.get('/api/metrics', async (req, res) => {
  const days = Math.min(90, Math.max(1, Number(req.query.days) || 7))
  res.json({ metrics: await traces.aggregate(days) })
})

// 隐式反馈:画布上删除生成结果 → 保留率信号(与 trace.imageId 关联)
app.post('/api/feedback', express.json(), async (req, res) => {
  const body = (req.body ?? {}) as { imageId?: unknown; action?: unknown; model?: unknown }
  if (typeof body.imageId !== 'string' || body.imageId.length > 64) {
    return res.status(400).json({ error: 'imageId 必须是非空字符串' })
  }
  if (body.action !== 'deleted') {
    return res.status(400).json({ error: 'action 仅支持 deleted' })
  }
  await traces.append({
    ts: new Date().toISOString(),
    kind: 'feedback',
    action: 'deleted',
    imageId: body.imageId,
    model: typeof body.model === 'string' ? body.model.slice(0, 200) : undefined,
  })
  return res.json({ ok: true })
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

// BYOK 配置(F8/S2):读取打码视图;热更新只写 config.json 并热切换后端;
// 环境变量优先级不变(env 覆盖的字段文件照写,但生效值仍来自 env,响应中提示)。
app.get('/api/config', (_req, res) => {
  res.json({ config: publicConfig(config) })
})

app.post('/api/config', async (req, res) => {
  const patch = applyConfigPatch(config, (req.body ?? {}) as ConfigPatch, {
    SWARMUI_PROVIDER: process.env.SWARMUI_PROVIDER,
    SWARMUI_COMFY_URL: process.env.SWARMUI_COMFY_URL,
  })
  if (!patch.ok) {
    return res.status(400).json({ error: patch.error })
  }
  try {
    await saveConfigPatch(rootDir, patch.filePatch)
  } catch (err) {
    return res.status(500).json({ error: `配置写入失败:${(err as Error).message}` })
  }
  const previousProvider = provider.name
  config = patch.config
  const switched = patch.filePatch.provider !== undefined || patch.filePatch.comfyUrl !== undefined
  if (switched) {
    provider = buildProvider(config)
    jobs.setProvider(provider)
    void refreshStatus()
  }
  logger.info('配置已更新', {
    switched,
    fields: Object.keys(patch.filePatch),
    envOverridden: patch.envOverridden,
  })
  return res.json({
    config: publicConfig(config),
    providerSwitched: switched,
    previousProvider,
    envOverridden: patch.envOverridden,
  })
})

// 测试连接(S2a/S2b):用候选配置构建临时后端探测,不落盘、不切换
app.post('/api/config/test', async (req, res) => {
  const body = (req.body ?? {}) as ConfigPatch
  const candidate = applyConfigPatch(config, body, {})
  if (!candidate.ok) {
    return res.status(400).json({ error: candidate.error })
  }
  const merged = { ...config, ...candidate.filePatch }
  if (merged.provider === 'cloud' && !merged.imageApiKey) {
    // status() 不发网络请求(避免计费),Key 缺失在这里直接给出结论
    return res.json({ ok: false, detail: '未配置图像 API Key', provider: 'cloud' })
  }
  const probe = buildProvider(merged)
  const status = await Promise.race([
    probe.status().catch((err: Error) => ({ ok: false, detail: `连接失败:${err.message}` })),
    new Promise<BackendStatus>((resolve) =>
      setTimeout(() => resolve({ ok: false, detail: '连接超时(5 秒)' }), 5000),
    ),
  ])
  return res.json({ ok: status.ok, detail: status.detail, provider: probe.name })
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
  const maskParsed = parseMaskImage((req.body as Record<string, unknown>).maskImage)
  if (!maskParsed.ok) {
    return res.status(400).json({ error: maskParsed.error })
  }
  if (maskParsed.mask && !initParsed.image) {
    return res.status(400).json({ error: '局部重绘需要同时携带参考图(initImage)' })
  }
  if (maskParsed.mask) {
    // 蒙版标记由服务端写入,不信任客户端声明;用于历史与前端展示
    parsed.params.inpaint = true
  }
  // 画布占位对象引用:事件可能先于本响应到达,客户端靠 clientRef 对齐
  const clientRefRaw = (req.body as Record<string, unknown>).clientRef
  const clientRef =
    typeof clientRefRaw === 'string' && clientRefRaw.trim()
      ? clientRefRaw.trim().slice(0, 64)
      : undefined
  const job = jobs.createJob(parsed.params, initParsed.image, maskParsed.mask, clientRef)
  logger.info('已接受生成任务', {
    jobId: job.id,
    kind: parsed.params.kind,
    batch: parsed.params.batchCount,
    inpaint: Boolean(maskParsed.mask),
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
  const kind = req.query.kind === 'image' || req.query.kind === 'video' ? req.query.kind : undefined
  const starred = req.query.starred === '1' ? true : req.query.starred === '0' ? false : undefined
  res.json({
    images: store.list(limit, {
      q: typeof req.query.q === 'string' ? req.query.q : undefined,
      model: typeof req.query.model === 'string' ? req.query.model : undefined,
      kind,
      starred,
    }),
  })
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

// 二进制资产上传:画布参考图 / 蒙版等大文件走原始字节,替换 base64 JSON,避开 body 限制
app.post('/api/assets', express.raw({ type: () => true, limit: '40mb' }), async (req, res) => {
  const body = req.body as Buffer | undefined
  if (!Buffer.isBuffer(body) || body.length === 0) {
    return res.status(400).json({ error: '请求体必须是非空二进制内容' })
  }
  const url = new URL(req.originalUrl, 'http://localhost')
  const extRaw = url.searchParams.get('ext')
  const kindRaw = url.searchParams.get('kind')
  const kind = kindRaw === 'video' ? 'video' : 'image'
  // 扩展名缺省时从 Content-Type 推断(如 image/png → png)
  let ext = extRaw ?? ''
  if (!ext && req.headers['content-type']) {
    const mime = req.headers['content-type'].split(';')[0].trim()
    ext = mime.split('/')[1] ?? ''
  }
  try {
    const record = await assets.save(body, ext, kind)
    return res.status(201).json({ asset: record, urls: assets.urls(record.id) })
  } catch (err) {
    return res.status(400).json({ error: (err as Error).message })
  }
})

app.get('/api/assets/:id', (req, res) => {
  const record = assets.get(req.params.id)
  if (!record) {
    return res.status(404).json({ error: '资产不存在' })
  }
  return res.json({ asset: record, urls: assets.urls(record.id) })
})

// 画布文档 CRUD:客户端防抖全量快照保存;POST 保存路径同时兼容 sendBeacon 冲刷
app.get('/api/documents', async (_req, res) => {
  res.json({ documents: await documents.listAll() })
})

app.post('/api/documents', async (req, res) => {
  const doc = await documents.create((req.body as Record<string, unknown>)?.name)
  return res.status(201).json({ document: doc })
})

app.get('/api/documents/:id', async (req, res) => {
  const doc = await documents.get(req.params.id)
  if (!doc) {
    return res.status(404).json({ error: '文档不存在' })
  }
  return res.json({ document: doc })
})

app.post('/api/documents/:id', async (req, res) => {
  try {
    const doc = await documents.save(req.params.id, req.body)
    if (!doc) {
      return res.status(404).json({ error: '文档不存在(id 非法)' })
    }
    return res.json({ document: doc })
  } catch (err) {
    return res.status(400).json({ error: (err as Error).message })
  }
})

app.delete('/api/documents/:id', async (req, res) => {
  const removed = await documents.remove(req.params.id)
  if (!removed) {
    return res.status(404).json({ error: '文档不存在' })
  }
  return res.json({ removed: true })
})

// SSE 实时推送:任务进度 + 图片落盘事件,连接即收到快照
app.get('/api/events', (req, res) => {
  res.set({
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    Connection: 'keep-alive',
  })
  res.flushHeaders()
  // 断线重连间隔提示(PRD §8 非功能:5s 内重连;EventSource 原生重连按此间隔)
  res.write('retry: 3000\n\n')
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
app.use('/assets', express.static(path.join(dataDir, 'assets'), { maxAge: '1y', immutable: true }))
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
    if (err.type === 'entity.too.large') {
      return res.status(413).json({ error: '上传内容超过大小限制(40MB)' })
    }
    logger.error(`未处理错误 ${req.method} ${req.originalUrl}`, { err })
    return res.status(500).json({ error: '服务器内部错误' })
  },
)

await store.init()
await assets.init()
await documents.init()
await jobs.restore()
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
  // 优雅关闭:任务快照与进行中的请求先冲刷再退出
  void jobs.flush().finally(() => {
    server.close(() => {
      logger.info('服务已关闭')
      process.exit(0)
    })
    // 兜底:5 秒后强制退出,避免残留连接悬挂进程
    setTimeout(() => process.exit(1), 5000).unref()
  })
}

process.on('SIGINT', () => shutdown('SIGINT'))
process.on('SIGTERM', () => shutdown('SIGTERM'))
