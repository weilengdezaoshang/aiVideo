import express from 'express'
import path from 'node:path'
import { randomUUID } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { applyConfigPatch, loadConfig, publicConfig, saveConfigPatch } from './config.js'
import type { AppConfig, ConfigPatch } from './config.js'
import { logger } from './logger.js'
import { AssetStore } from './assets.js'
import { DocumentConflictError, DocumentStore } from './documents.js'
import { JobManager } from './services/job-manager.js'
import { planDirections } from './services/direction-planner.js'
import { AgentStore } from './services/agent/agent-store.js'
import { planAgentAction } from './services/agent/agent-planner.js'
import {
  CloudImagesProvider,
  hasChinese,
  resolveCloudImageModel,
  translateToEnglish,
} from './services/providers/cloud-provider.js'
import { ComfyUIProvider } from './services/providers/comfyui-provider.js'
import { MockProvider } from './services/providers/mock-provider.js'
import type { GenerationProvider } from './services/providers/provider.js'
import { Store } from './store.js'
import { classifyError, estimateCostCny, promptHash, TraceStore } from './traces.js'
import type { BackendStatus, Job } from './types.js'
import { parseGenParams, parseInitImage, parseMaskImage } from './validate.js'
import { RembgCutoutProvider } from './services/cutout/rembg-provider.js'
import { applyCutoutMask } from './services/cutout/apply-mask.js'

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
const cutout = new RembgCutoutProvider({
  serviceUrl: process.env.CUTOUT_SERVICE_URL || 'http://127.0.0.1:7100',
  model: process.env.CUTOUT_MODEL || 'birefnet-portrait',
})
const documents = new DocumentStore(path.join(dataDir, 'documents'))
const jobs = new JobManager(provider, store, path.join(dataDir, 'jobs.json'))
// Agent 会话(用例 A):计划与人工暂停点跨重启可恢复
const agentSessions = new AgentStore(path.join(dataDir, 'agent-sessions.json'))
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
    translated: Boolean(job.params.sourcePrompt),
    promptHash: promptHash(job.params.sourcePrompt || job.params.prompt),
    promptChars: (job.params.sourcePrompt || job.params.prompt).length,
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
  config = loadConfig(rootDir)
  for (const field of Object.keys(patch.filePatch) as Array<keyof typeof patch.filePatch>) {
    if (config[field] !== patch.filePatch[field] && !patch.envOverridden.includes(field)) {
      patch.envOverridden.push(field)
    }
  }
  const switched = [
    'provider',
    'comfyUrl',
    'cloudVendor',
    'cloudBaseUrl',
    'cloudModel',
    'imageApiKey',
  ].some((field) => field in patch.filePatch)
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
  const merged = candidate.config
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

app.post('/api/generate', async (req, res) => {
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
  // 提示词中译英(可评测):仅云端 + 配置了文本模型 + 含中文时触发;
  // 确定性翻译不改写语义;失败降级原文,不阻塞生成。
  if (
    provider.name === 'cloud' &&
    config.cloudVendor !== 'aliyun' &&
    config.cloudTextModel &&
    hasChinese(parsed.params.prompt)
  ) {
    try {
      const sourcePrompt = parsed.params.prompt
      parsed.params.prompt = await translateToEnglish({
        apiKey: config.imageApiKey,
        baseUrl: config.cloudBaseUrl,
        model: config.cloudTextModel,
        prompt: sourcePrompt,
        signal: AbortSignal.timeout(15_000),
      })
      parsed.params.sourcePrompt = sourcePrompt
      logger.info('提示词已中译英', { chars: sourcePrompt.length })
    } catch (err) {
      logger.warn('翻译失败,使用原文继续', { err })
    }
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
  if (provider.name === 'cloud' && parsed.params.kind === 'image') {
    // 队列、历史和追踪记录必须与实际发给上游的模型一致。
    parsed.params.model = resolveCloudImageModel(
      config.cloudVendor,
      config.cloudModel,
      Boolean(initParsed.image && !maskParsed.mask),
    )
  }
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

// 四方向生成(PRD §5):一个需求 → 规划器输出四条结构化方向 → 四个独立任务(同组)。
// 每方向一个 jobId,前端四占位同步呈现;服务端只保证"同组同源、方向互异"。
app.post('/api/generate/group', async (req, res) => {
  if (jobs.listActive().length + 4 > MAX_ACTIVE_JOBS) {
    return res.status(429).json({ error: `队列已满(${MAX_ACTIVE_JOBS} 个进行中任务),请稍后再试` })
  }
  const body = (req.body ?? {}) as Record<string, unknown>
  const prompt = typeof body.prompt === 'string' ? body.prompt.trim() : ''
  if (!prompt) {
    return res.status(400).json({ error: '提示词(prompt)不能为空' })
  }
  if (prompt.length > 4000) {
    return res.status(400).json({ error: '提示词过长(上限 4000 字符)' })
  }
  const clientRefs = Array.isArray(body.clientRefs) ? body.clientRefs : []
  if (
    clientRefs.length !== 4 ||
    clientRefs.some((r) => typeof r !== 'string' || !r.trim() || r.length > 64)
  ) {
    return res.status(400).json({ error: 'clientRefs 必须是 4 个非空字符串(每个 ≤64 字符)' })
  }
  const plan = planDirections(prompt)
  const groupId = randomUUID()
  // 两阶段:先完成全部方向的任务参数校验,再统一入队,避免校验中途失败留下半组任务。
  // 四方向仅面向图片(PRD §5):即使请求携带 kind=video 也强制为 image。
  const preparedSlots = plan.directions.map((direction) => {
    const parsed = parseGenParams({
      ...body,
      prompt: direction.prompt,
      kind: 'image',
      batchCount: 1,
      seed: -1,
    })
    if (!parsed.ok) {
      return { ok: false as const, error: parsed.error }
    }
    if (provider.name === 'cloud' && parsed.params.kind === 'image') {
      parsed.params.model = resolveCloudImageModel(config.cloudVendor, config.cloudModel, false)
    }
    return {
      ok: true as const,
      params: parsed.params,
      clientRef: (clientRefs[direction.slot] as string).trim().slice(0, 64),
      direction,
    }
  })
  for (const slot of preparedSlots) {
    if (!slot.ok) {
      return res.status(400).json({ error: slot.error })
    }
  }
  const slots = []
  for (const slot of preparedSlots) {
    if (!slot.ok) {
      continue
    }
    const job = jobs.createJob(slot.params, undefined, undefined, slot.clientRef, {
      groupId,
      slot: slot.direction.slot,
      directionTitle: slot.direction.title,
    })
    slots.push({
      slot: slot.direction.slot,
      title: slot.direction.title,
      dimension: slot.direction.dimension,
      prompt: slot.direction.prompt,
      jobId: job.id,
      clientRef: slot.clientRef,
    })
    logger.info('四方向任务已入组', { groupId, slot: slot.direction.slot, jobId: job.id })
  }
  return res.status(202).json({
    groupId,
    subject: plan.subject,
    constraints: plan.constraints,
    slots,
  })
})

// Agent 会话(用例 A):显式引用 + 请求 → 计划;没有引用时不执行(PRD §7)
app.post('/api/agent/sessions', async (req, res) => {
  const body = (req.body ?? {}) as Record<string, unknown>
  const docId = typeof body.docId === 'string' ? body.docId : ''
  const assetId = typeof body.assetId === 'string' ? body.assetId.trim() : ''
  const request = typeof body.request === 'string' ? body.request.trim().slice(0, 4000) : ''
  if (!docId) {
    return res.status(400).json({ error: '缺少 docId' })
  }
  if (!request) {
    return res.status(400).json({ error: '请求内容(request)不能为空' })
  }
  if (!assetId) {
    // 零工具调用:缺显式引用时先要求选图
    return res.status(422).json({
      error: '请先在画布选择一张图片,点"添加到对话"后再让助手执行',
      reason: 'need_reference',
    })
  }
  const asset = assets.get(assetId)
  if (!asset || asset.kind !== 'image') {
    return res.status(404).json({ error: '引用的图片资产不存在' })
  }
  // 模型规划:与中译英同一开关条件(云端文本模型可用时);失败降级规则规划
  const useModel =
    provider.name === 'cloud' && config.cloudVendor !== 'aliyun' && Boolean(config.cloudTextModel)
  const plan = await planAgentAction(request, {
    apiKey: useModel ? config.imageApiKey : undefined,
    baseUrl: useModel ? config.cloudBaseUrl : undefined,
    model: useModel ? config.cloudTextModel : undefined,
  })
  if (!plan.ok) {
    return res.status(422).json({ error: plan.message, reason: plan.reason })
  }
  const session = agentSessions.create({
    docId,
    assetId,
    assetExt: asset.ext,
    request,
    plan: plan.plan,
    plannedBy: plan.plannedBy,
  })
  logger.info('Agent 会话已创建', { sessionId: session.id, plannedBy: plan.plannedBy })
  return res.status(201).json({ session })
})

app.get('/api/agent/sessions', (req, res) => {
  const docId = typeof req.query.docId === 'string' ? req.query.docId : ''
  if (!docId) {
    return res.status(400).json({ error: '缺少 docId 查询参数' })
  }
  return res.json({ sessions: agentSessions.listByDoc(docId) })
})

app.get('/api/agent/sessions/:id', (req, res) => {
  const session = agentSessions.get(req.params.id)
  if (!session) {
    return res.status(404).json({ error: 'Agent 会话不存在' })
  }
  return res.json({ session })
})

// 准备蒙版(自动、最多一次):识别成功进入 waiting_mask;
// 重复调用直接返回现态(恢复后不自动识别第二次,PRD §7)
app.post('/api/agent/sessions/:id/prepare', async (req, res) => {
  const session = agentSessions.get(req.params.id)
  if (!session) {
    return res.status(404).json({ error: 'Agent 会话不存在' })
  }
  if (session.status === 'waiting_mask' || session.status === 'completed') {
    return res.json({ session })
  }
  if (session.status !== 'planned') {
    return res.status(409).json({ error: `会话状态 ${session.status} 不允许准备蒙版`, session })
  }
  const source = await assets.read(session.assetId)
  if (!source) {
    agentSessions.update(session.id, { status: 'failed', error: '源图资产已不存在' })
    return res.status(404).json({ error: '源图资产已不存在' })
  }
  agentSessions.update(session.id, { status: 'preparing' })
  try {
    const contentType = source.record.ext === 'jpg' ? 'image/jpeg' : `image/${source.record.ext}`
    const result = await cutout.createMask(source.data, contentType, AbortSignal.timeout(120_000))
    const mask = await assets.save(result.data, 'png', 'image')
    const updated = agentSessions.update(session.id, {
      status: 'waiting_mask',
      maskAssetId: mask.id,
    })
    return res.json({ session: updated, mask, urls: assets.urls(mask.id) })
  } catch (err) {
    logger.warn('Agent 蒙版准备失败', { sessionId: session.id, err })
    const updated = agentSessions.update(session.id, {
      status: 'failed',
      error: (err as Error).message || '主体识别失败',
    })
    return res.status(502).json({ error: (err as Error).message, session: updated })
  }
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

// 资产列表("导入资产"抽屉):新的在前,可按类型过滤
app.get('/api/assets', (req, res) => {
  const kind = req.query.kind === 'video' ? 'video' : req.query.kind === 'image' ? 'image' : undefined
  const limit = Math.min(500, Math.max(1, Number(req.query.limit) || 200))
  const items = assets.list(kind, limit).map((record) => ({
    asset: record,
    urls: assets.urls(record.id),
  }))
  return res.json({ assets: items })
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

// 主体识别只生成可编辑 Mask，不改变原图；模型由 CutoutProvider 隔离。
app.post('/api/cutout/detect/:assetId', async (req, res) => {
  const source = await assets.read(req.params.assetId)
  if (!source || source.record.kind !== 'image') {
    return res.status(404).json({ error: '原图资产不存在' })
  }
  try {
    const contentType = source.record.ext === 'jpg' ? 'image/jpeg' : `image/${source.record.ext}`
    const result = await cutout.createMask(source.data, contentType, AbortSignal.timeout(120_000))
    const mask = await assets.save(result.data, 'png', 'image')
    return res.json({
      mask,
      urls: assets.urls(mask.id),
      provider: cutout.name,
      model: result.model,
    })
  } catch (err) {
    logger.warn('主体识别失败', { assetId: req.params.assetId, err })
    return res.status(502).json({ error: (err as Error).message })
  }
})

// 用户确认编辑后的 Mask 后执行像素合成；无需调用生成大模型。
// Agent 用例 A 的显式提交走同一端点:?sessionId= 标记本次提交来自会话,
// 服务端校验会话必须处于 waiting_mask 且引用一致,完成后回写结果资产。
app.post(
  '/api/cutout/apply/:assetId',
  express.raw({ type: () => true, limit: '20mb' }),
  async (req, res) => {
    const source = await assets.read(req.params.assetId)
    if (!source || source.record.kind !== 'image') {
      return res.status(404).json({ error: '原图资产不存在' })
    }
    const sessionId = typeof req.query.sessionId === 'string' ? req.query.sessionId : ''
    let session = undefined
    if (sessionId) {
      session = agentSessions.get(sessionId)
      if (!session) {
        return res.status(404).json({ error: 'Agent 会话不存在' })
      }
      if (session.status !== 'waiting_mask' || session.assetId !== req.params.assetId) {
        return res.status(409).json({ error: 'Agent 会话不在等待蒙版确认状态' })
      }
    }
    const mask = req.body as Buffer | undefined
    if (!Buffer.isBuffer(mask) || mask.length === 0) {
      return res.status(400).json({ error: '请求体必须是 PNG Mask' })
    }
    try {
      const output = await applyCutoutMask(source.data, mask)
      const asset = await assets.save(output, 'png', 'image')
      const savedMask = await assets.save(mask, 'png', 'image')
      if (session) {
        agentSessions.update(session.id, {
          status: 'completed',
          resultAssetId: asset.id,
          resultExt: asset.ext,
        })
      }
      return res.status(201).json({ asset, mask: savedMask, urls: assets.urls(asset.id) })
    } catch (err) {
      return res.status(400).json({ error: (err as Error).message })
    }
  },
)

// 画布文档 CRUD:客户端防抖全量快照保存;POST 保存路径同时兼容 sendBeacon 冲刷
app.get('/api/documents', async (_req, res) => {
  res.json({ documents: await documents.listAll() })
})

// 幂等创建(PRD §3 A01):同一 Idempotency-Key 只产出一份文档,
// 连续点击 / 超时重试 / 丢响应后重发都返回第一次的文档
app.post('/api/documents', async (req, res) => {
  const key = req.header('idempotency-key')?.trim()
  if (key && key.length > 128) {
    return res.status(400).json({ error: 'Idempotency-Key 过长(上限 128 字符)' })
  }
  const doc = await documents.create((req.body as Record<string, unknown>)?.name, key || undefined)
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
    if (err instanceof DocumentConflictError) {
      // 乐观并发冲突(PRD §9 A13):不覆盖服务端新结果,把服务端版本交还客户端
      return res.status(409).json({ error: err.message, document: err.serverDoc })
    }
    return res.status(400).json({ error: (err as Error).message })
  }
})

// 重命名:只动名称,不需要全量快照,也绕开内容冲突
app.put('/api/documents/:id/rename', async (req, res) => {
  const name = (req.body as Record<string, unknown> | undefined)?.name
  const doc = await documents.rename(req.params.id, name)
  if (!doc) {
    return res.status(404).json({ error: '文档不存在' })
  }
  return res.json({ document: doc })
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

// 页面路由(PRD §3):介绍页 / · 工作台 /workspace · 编辑器 /canvas?doc=<id> ·
// 旧版实验入口 /legacy。静态目录兜底 /index.html 等直链。
const sendPage = (relPath: string) => (_req: express.Request, res: express.Response) => {
  res.sendFile(path.join(rootDir, 'web', relPath))
}
app.get('/', sendPage(path.join('landing', 'index.html')))
app.get('/workspace', sendPage(path.join('workspace', 'index.html')))
app.get('/canvas', sendPage(path.join('canvas', 'index.html')))
app.get('/legacy', sendPage('index.html'))

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
await agentSessions.init()
await jobs.restore()
await refreshStatus()
const server = app.listen(config.port, '127.0.0.1', () => {
  logger.info(`SwarmUI-MVP 已启动: http://127.0.0.1:${config.port}`)
  logger.info(
    `provider=${provider.name}${config.provider === 'comfyui' ? ` comfyUrl=${config.comfyUrl}` : ''}`,
  )
  logger.info(`后端状态: ${providerStatus.detail}`)
})

function shutdown(signal: string): void {
  logger.info(`收到 ${signal},正在关闭`)
  // 优雅关闭:任务快照与进行中的请求先冲刷再退出
  void Promise.all([jobs.flush(), agentSessions.flush()]).finally(() => {
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
