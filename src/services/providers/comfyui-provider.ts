import { randomUUID } from 'node:crypto'
import { logger } from '../../logger.js'
import type { BackendStatus, GenParams, InitImage, ModelInfo, SamplerOptions } from '../../types.js'
import type { GenContext, GeneratedImage, GenerationProvider } from './provider.js'

interface ComfyOutputFile {
  filename: string
  subfolder: string
  type: string
}

interface ComfyHistoryEntry {
  outputs?: Record<string, { images?: ComfyOutputFile[]; videos?: ComfyOutputFile[] }>
  status?: { status_str?: string }
}

type ComfyWorkflow = Record<string, { class_type: string; inputs: Record<string, unknown> }>

export type VideoBackend = 'ltxv' | 'wan' | 'wan-dual'

/** ComfyUI 对接参数;超时与视频模型均可由 config.json / 环境变量覆盖。 */
export interface ComfyUIOptions {
  /** 图像任务整体超时,默认 20 分钟 */
  imageTimeoutMs?: number
  /** 视频任务整体超时,默认 60 分钟 */
  videoTimeoutMs?: number
  /** 视频模型文件名;留空则从后端模型列表自动探测(Wan 优先于 LTX-Video) */
  videoModel?: string
  /** 视频工作流;auto 时按模型名推断(wan → Wan,ltx → LTXV) */
  videoBackend?: 'auto' | VideoBackend
  /** Wan 系文本编码器文件(models/text_encoders),UNETLoader 链路使用 */
  wanClip?: string
  /** Wan 系 VAE 文件(models/vae) */
  wanVae?: string
  /** Wan2.2 高噪 UNet;与 wanLowNoiseUnet 同时配置时走双模型分段采样链路 */
  wanHighNoiseUnet?: string
  /** Wan2.2 低噪 UNet */
  wanLowNoiseUnet?: string
  /** 以下两项供测试注入,生产不传 */
  fetchImpl?: typeof fetch
  pollMs?: number
}

const DEFAULT_IMAGE_TIMEOUT_MS = 20 * 60 * 1000
const DEFAULT_VIDEO_TIMEOUT_MS = 60 * 60 * 1000

/** 真实生成后端:走 ComfyUI HTTP API(/prompt 提交工作流 + 轮询 /history + /view 取产物)。 */
export class ComfyUIProvider implements GenerationProvider {
  readonly name = 'comfyui'
  readonly capacity = 1

  private readonly fetchFn: typeof fetch
  private readonly pollMs: number
  private readonly imageTimeoutMs: number
  private readonly videoTimeoutMs: number
  private readonly videoOpts: ComfyUIOptions

  constructor(
    private baseUrl: string,
    opts: ComfyUIOptions = {},
  ) {
    this.fetchFn = opts.fetchImpl ?? fetch
    this.pollMs = opts.pollMs ?? 700
    this.imageTimeoutMs = opts.imageTimeoutMs ?? DEFAULT_IMAGE_TIMEOUT_MS
    this.videoTimeoutMs = opts.videoTimeoutMs ?? DEFAULT_VIDEO_TIMEOUT_MS
    this.videoOpts = opts
  }

  private async fetchJson<T>(
    pathName: string,
    init: RequestInit & { timeoutMs?: number } = {},
  ): Promise<T> {
    const { timeoutMs = 5000, ...rest } = init
    const res = await this.fetchFn(`${this.baseUrl}${pathName}`, {
      ...rest,
      signal: rest.signal ?? AbortSignal.timeout(timeoutMs),
    })
    if (!res.ok) {
      // /prompt 校验失败时 ComfyUI 会返回 { error, node_errors },带上细节便于定位工作流问题
      const body = (await res.json().catch(() => undefined)) as { error?: string } | undefined
      const detail = body?.error ? `:${body.error}` : ''
      throw new Error(`ComfyUI ${pathName} 返回 HTTP ${res.status}${detail}`)
    }
    return (await res.json()) as T
  }

  async status(): Promise<BackendStatus> {
    try {
      const stats = await this.fetchJson<{ system?: { comfyui_version?: string } }>(
        '/system_stats',
        { timeoutMs: 2500 },
      )
      return {
        ok: true,
        detail: `ComfyUI ${stats.system?.comfyui_version ?? ''} @ ${this.baseUrl}`,
      }
    } catch (err) {
      return { ok: false, detail: `无法连接 ComfyUI(${(err as Error).message}),请先启动 ComfyUI` }
    }
  }

  async listModels(): Promise<ModelInfo[]> {
    const names = await this.listNodeChoices('CheckpointLoaderSimple', 'ckpt_name')
    return names.map((n) => ({ id: n, name: n }))
  }

  async listSamplerOptions(): Promise<SamplerOptions> {
    const required = await this.listNodeRequired('KSampler')
    return {
      samplers: (required?.sampler_name?.[0] as string[] | undefined) ?? ['euler'],
      schedulers: (required?.scheduler?.[0] as string[] | undefined) ?? ['normal'],
    }
  }

  /** 读取 ComfyUI 节点的某个枚举输入可选值(如 checkpoint 文件名列表)。 */
  private async listNodeChoices(node: string, input: string): Promise<string[]> {
    const required = await this.listNodeRequired(node)
    return (required?.[input]?.[0] as string[] | undefined) ?? []
  }

  private async listNodeRequired(node: string): Promise<Record<string, unknown[]> | undefined> {
    const info = await this.fetchJson<
      Record<string, { input?: { required?: Record<string, unknown[]> } }>
    >(`/object_info/${node}`)
    return info[node]?.input?.required
  }

  async generateVideo(
    params: GenParams,
    ctx: GenContext,
    initImage?: InitImage,
  ): Promise<GeneratedImage> {
    const resolved = await this.resolveVideoModel()
    ctx.onProgress(0.01, `视频模型 ${resolved.model}`)
    let refName: string | undefined
    if (initImage) {
      ctx.onProgress(0.02, '上传首帧到 ComfyUI')
      refName = await this.uploadImage(initImage)
    }
    const workflow =
      resolved.backend === 'ltxv'
        ? buildLtxvVideoWorkflow(params, ctx.seed, resolved.model, refName)
        : buildWanVideoWorkflow(
            params,
            ctx.seed,
            {
              model: resolved.model,
              lowNoise: resolved.lowNoise,
              clip: this.videoOpts.wanClip ?? DEFAULT_WAN_CLIP,
              vae: this.videoOpts.wanVae ?? DEFAULT_WAN_VAE,
            },
            refName,
          )
    ctx.onProgress(0.03, '提交视频工作流到 ComfyUI')
    const submit = await this.fetchJson<{ prompt_id: string }>('/prompt', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt: workflow, client_id: randomUUID() }),
    })
    logger.debug('已提交视频工作流到 ComfyUI', {
      promptId: submit.prompt_id,
      backend: resolved.backend,
      seed: ctx.seed,
    })
    const files = await this.pollForResult(submit.prompt_id, ctx, {
      timeoutMs: this.videoTimeoutMs,
      estimateMs: 60_000 + params.steps * params.durationSec * params.fps * 120,
      label: 'ComfyUI 生成视频中',
    })
    const out = pickVideoFile(files)
    if (!out) {
      throw new Error('ComfyUI 未返回视频文件,请确认工作流的 SaveVideo 节点已启用')
    }
    const data = await this.downloadFile(out)
    ctx.onProgress(1, '生成完成')
    logger.debug('ComfyUI 视频完成', { promptId: submit.prompt_id, file: out.filename })
    return { data, ext: extOf(out.filename) ?? 'mp4' }
  }

  async generate(
    params: GenParams,
    ctx: GenContext,
    initImage?: InitImage,
  ): Promise<GeneratedImage> {
    let refName: string | undefined
    if (initImage) {
      ctx.onProgress(0.01, '上传参考图到 ComfyUI')
      refName = await this.uploadImage(initImage)
    }
    ctx.onProgress(0.02, '提交工作流到 ComfyUI')
    const submit = await this.fetchJson<{ prompt_id: string }>('/prompt', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        prompt: buildWorkflow(params, ctx.seed, refName),
        client_id: randomUUID(),
      }),
    })
    logger.debug('已提交工作流到 ComfyUI', { promptId: submit.prompt_id, seed: ctx.seed })
    const files = await this.pollForResult(submit.prompt_id, ctx, {
      timeoutMs: this.imageTimeoutMs,
      estimateMs: 15000 + params.steps * 600,
      label: 'ComfyUI 采样中',
    })
    const out = files.find((f) => /\.(png|jpe?g|webp)$/i.test(f.filename)) ?? files[0]
    const data = await this.downloadFile(out)
    ctx.onProgress(1, '生成完成')
    logger.debug('ComfyUI 出图完成', { promptId: submit.prompt_id, file: out.filename })
    return { data, ext: extOf(out.filename) ?? 'png' }
  }

  /** 提交后轮询 /history 直到产出文件、执行出错、超时或被取消。 */
  private async pollForResult(
    promptId: string,
    ctx: GenContext,
    opts: { timeoutMs: number; estimateMs: number; label: string },
  ): Promise<ComfyOutputFile[]> {
    const startedAt = Date.now()
    for (;;) {
      if (Date.now() - startedAt > opts.timeoutMs) {
        throw new Error(`等待 ComfyUI 超时(${Math.round(opts.timeoutMs / 60000)} 分钟)`)
      }
      if (ctx.signal.aborted) {
        await this.interrupt()
        throw new Error('已取消')
      }
      await sleep(this.pollMs)
      const elapsed = Date.now() - startedAt
      ctx.onProgress(
        Math.min(0.9, 0.05 + (elapsed / opts.estimateMs) * 0.85),
        `${opts.label} ${Math.round(elapsed / 1000)}s`,
      )
      const history = await this.fetchJson<Record<string, ComfyHistoryEntry>>(
        `/history/${promptId}`,
        { timeoutMs: 10000 },
      )
      const entry = history[promptId]
      if (!entry) {
        continue
      }
      if (entry.status?.status_str === 'error') {
        throw new Error('ComfyUI 执行出错,请查看其日志')
      }
      const files = collectOutputFiles(entry)
      if (files.length > 0) {
        return files
      }
    }
  }

  /** 按配置解析视频模型:显式配置 > 双 UNet > 名称探测 > 自动扫描。 */
  private async resolveVideoModel(): Promise<{
    backend: VideoBackend
    model: string
    lowNoise?: string
  }> {
    const o = this.videoOpts
    if (o.wanHighNoiseUnet && o.wanLowNoiseUnet) {
      return { backend: 'wan-dual', model: o.wanHighNoiseUnet, lowNoise: o.wanLowNoiseUnet }
    }
    const wantBackend = o.videoBackend && o.videoBackend !== 'auto' ? o.videoBackend : undefined
    if (o.videoModel) {
      const name = o.videoModel
      const backend: VideoBackend = wantBackend ?? (/wan/i.test(name) ? 'wan' : 'ltxv')
      return { backend, model: name }
    }
    const [checkpoints, unets] = await Promise.all([
      this.listNodeChoices('CheckpointLoaderSimple', 'ckpt_name'),
      this.listNodeChoices('UNETLoader', 'unet_name'),
    ])
    const ltx = checkpoints.find((n) => /ltx/i.test(n))
    const wan =
      unets.find((n) => /wan/i.test(n) && /i2v/i.test(n)) ?? unets.find((n) => /wan/i.test(n))
    if (wantBackend === 'wan') {
      if (!wan) {
        throw new Error(
          '未在 ComfyUI models/unet 中找到 Wan 视频模型,请放置 wan 系列模型或配置 videoModel',
        )
      }
      return { backend: 'wan', model: wan }
    }
    if (wantBackend === 'ltxv') {
      if (!ltx) {
        throw new Error(
          '未在 ComfyUI models/checkpoints 中找到 LTX-Video 模型,请放置 ltx 系列模型或配置 videoModel',
        )
      }
      return { backend: 'ltxv', model: ltx }
    }
    if (wan) {
      return { backend: 'wan', model: wan }
    }
    if (ltx) {
      return { backend: 'ltxv', model: ltx }
    }
    throw new Error(
      '未找到可用的视频模型:请将 Wan 模型放入 models/unet,或 LTX-Video 模型放入 models/checkpoints,或在 config.json 中配置 videoModel',
    )
  }

  private async downloadFile(out: ComfyOutputFile): Promise<Buffer> {
    const query = `filename=${encodeURIComponent(out.filename)}&subfolder=${encodeURIComponent(out.subfolder)}&type=${encodeURIComponent(out.type)}`
    const res = await this.fetchFn(`${this.baseUrl}/view?${query}`, {
      signal: AbortSignal.timeout(60000),
    })
    if (!res.ok) {
      throw new Error(`下载生成结果失败:HTTP ${res.status}`)
    }
    return Buffer.from(await res.arrayBuffer())
  }

  private async uploadImage(image: InitImage): Promise<string> {
    const mime =
      image.ext === 'png' ? 'image/png' : image.ext === 'webp' ? 'image/webp' : 'image/jpeg'
    const form = new FormData()
    form.append(
      'image',
      new Blob([new Uint8Array(image.data)], { type: mime }),
      `swarm-ref-${Date.now()}.${image.ext}`,
    )
    form.append('overwrite', 'true')
    const res = await this.fetchFn(`${this.baseUrl}/upload/image`, {
      method: 'POST',
      body: form,
      signal: AbortSignal.timeout(30000),
    })
    if (!res.ok) {
      throw new Error(`上传参考图失败:HTTP ${res.status}`)
    }
    const body = (await res.json()) as { name?: string }
    if (!body.name) {
      throw new Error('上传参考图失败:ComfyUI 未返回文件名')
    }
    return body.name
  }

  private async interrupt(): Promise<void> {
    try {
      await this.fetchFn(`${this.baseUrl}/interrupt`, {
        method: 'POST',
        signal: AbortSignal.timeout(2000),
      })
    } catch (err) {
      // 尽力而为:ComfyUI 可能已经不在执行该任务
      logger.warn('中断 ComfyUI 任务失败', { err })
    }
  }
}

const DEFAULT_WAN_CLIP = 'umt5_xxl_fp8_e4m3fn_scaled.safetensors'
const DEFAULT_WAN_VAE = 'wan_2.1_vae.safetensors'

/**
 * 标准 txt2img / img2img 工作流:
 * txt2img: Checkpoint → CLIP 编码 → KSampler(denoise=1) → VAE 解码 → SaveImage
 * img2img: 参考图经 LoadImage + VAEEncode 进入潜在空间,denoise 由参数控制
 */
export function buildWorkflow(p: GenParams, seed: number, initImageName?: string): ComfyWorkflow {
  const latentSource = initImageName ? { node: '11', output: 0 } : { node: '5', output: 0 }
  const workflow: ComfyWorkflow = {
    '4': { class_type: 'CheckpointLoaderSimple', inputs: { ckpt_name: p.model } },
    '6': { class_type: 'CLIPTextEncode', inputs: { text: p.prompt, clip: ['4', 1] } },
    '7': { class_type: 'CLIPTextEncode', inputs: { text: p.negativePrompt, clip: ['4', 1] } },
    '3': {
      class_type: 'KSampler',
      inputs: {
        seed,
        steps: p.steps,
        cfg: p.cfgScale,
        sampler_name: p.sampler,
        scheduler: p.scheduler,
        denoise: initImageName ? p.denoise : 1,
        model: ['4', 0],
        positive: ['6', 0],
        negative: ['7', 0],
        latent_image: [latentSource.node, latentSource.output],
      },
    },
    '8': { class_type: 'VAEDecode', inputs: { samples: ['3', 0], vae: ['4', 2] } },
    '9': { class_type: 'SaveImage', inputs: { filename_prefix: 'SwarmUI_MVP', images: ['8', 0] } },
  }
  if (initImageName) {
    workflow['10'] = { class_type: 'LoadImage', inputs: { image: initImageName } }
    workflow['11'] = { class_type: 'VAEEncode', inputs: { pixels: ['10', 0], vae: ['4', 2] } }
  } else {
    workflow['5'] = {
      class_type: 'EmptyLatentImage',
      inputs: { width: p.width, height: p.height, batch_size: 1 },
    }
  }
  return workflow
}

const roundTo = (v: number, multiple: number) =>
  Math.max(multiple, Math.round(v / multiple) * multiple)
const videoFrames = (p: GenParams) => Math.max(1, Math.round(p.durationSec * p.fps))

/** 视频产物收尾:解码 → 按帧率封装 → 存 mp4。SaveAnimatedWEBP 兼容性更好但 <video> 无法播放,故用 SaveVideo。 */
function appendVideoOutput(workflow: ComfyWorkflow, decodedNode: string, fps: number): void {
  workflow['24'] = { class_type: 'CreateVideo', inputs: { images: [decodedNode, 0], fps } }
  workflow['25'] = {
    class_type: 'SaveVideo',
    inputs: { video: ['24', 0], filename_prefix: 'SwarmUI_MVP_video', format: 'mp4' },
  }
}

/**
 * LTX-Video 图生视频 / 文生视频(单 checkpoint):
 * i2v: LTXVImgToVideo(参考图作首帧)→ LTXVConditioning 注入帧率 → KSampler
 * t2v: EmptyLTXVLatentVideo → LTXVConditioning → KSampler
 * 约束:宽高为 32 的倍数,帧数满足 8n+1。
 */
export function buildLtxvVideoWorkflow(
  p: GenParams,
  seed: number,
  model: string,
  initImageName?: string,
): ComfyWorkflow {
  const width = roundTo(p.width, 32)
  const height = roundTo(p.height, 32)
  const length = 8 * Math.max(1, Math.round(videoFrames(p) / 8)) + 1
  const workflow: ComfyWorkflow = {
    '4': { class_type: 'CheckpointLoaderSimple', inputs: { ckpt_name: model } },
    '6': { class_type: 'CLIPTextEncode', inputs: { text: p.prompt, clip: ['4', 1] } },
    '7': { class_type: 'CLIPTextEncode', inputs: { text: p.negativePrompt, clip: ['4', 1] } },
    '3': {
      class_type: 'KSampler',
      inputs: {
        seed,
        steps: p.steps,
        cfg: p.cfgScale,
        sampler_name: p.sampler,
        scheduler: p.scheduler,
        denoise: 1,
        model: ['4', 0],
        positive: ['13', 0],
        negative: ['13', 1],
        latent_image: ['12', initImageName ? 2 : 0],
      },
    },
    '8': { class_type: 'VAEDecode', inputs: { samples: ['3', 0], vae: ['4', 2] } },
  }
  if (initImageName) {
    workflow['10'] = { class_type: 'LoadImage', inputs: { image: initImageName } }
    workflow['12'] = {
      class_type: 'LTXVImgToVideo',
      inputs: {
        positive: ['6', 0],
        negative: ['7', 0],
        vae: ['4', 2],
        image: ['10', 0],
        width,
        height,
        length,
        batch_size: 1,
      },
    }
    // i2v:帧率条件作用在 ImgToVideo 输出的条件上
    workflow['13'] = {
      class_type: 'LTXVConditioning',
      inputs: { frame_rate: p.fps, positive: ['12', 0], negative: ['12', 1] },
    }
  } else {
    workflow['12'] = {
      class_type: 'EmptyLTXVLatentVideo',
      inputs: { width, height, length, batch_size: 1 },
    }
    workflow['13'] = {
      class_type: 'LTXVConditioning',
      inputs: { frame_rate: p.fps, positive: ['6', 0], negative: ['7', 0] },
    }
  }
  appendVideoOutput(workflow, '8', p.fps)
  return workflow
}

/**
 * Wan 系图生视频 / 文生视频(UNETLoader 链路,支持 Wan2.1-I2V 与 Wan2.2 融合单文件模型):
 * UNET → ModelSamplingSD3(shift 8)→ WanImageToVideo(i2v)或 EmptyHunyuanLatentVideo(t2v)→ KSampler
 * 双 UNet(wanHighNoiseUnet + wanLowNoiseUnet):按 Wan2.2 官方模板,前后半程分段采样,SplitSigmas 在中点切换。
 * 约束:宽高为 16 的倍数,帧数为 4 的倍数。
 */
export function buildWanVideoWorkflow(
  p: GenParams,
  seed: number,
  opts: { model: string; lowNoise?: string; clip: string; vae: string },
  initImageName?: string,
): ComfyWorkflow {
  const width = roundTo(p.width, 16)
  const height = roundTo(p.height, 16)
  const length = Math.max(1, Math.round(videoFrames(p) / 4) * 4)
  const condSource = initImageName
    ? { positive: ['12', 0], negative: ['12', 1] }
    : { positive: ['6', 0], negative: ['7', 0] }
  const workflow: ComfyWorkflow = {
    '1': { class_type: 'UNETLoader', inputs: { unet_name: opts.model, weight_dtype: 'default' } },
    '2': { class_type: 'CLIPLoader', inputs: { clip_name: opts.clip, type: 'wan' } },
    '3': { class_type: 'VAELoader', inputs: { vae_name: opts.vae } },
    '6': { class_type: 'CLIPTextEncode', inputs: { text: p.prompt, clip: ['2', 0] } },
    '7': { class_type: 'CLIPTextEncode', inputs: { text: p.negativePrompt, clip: ['2', 0] } },
    '14': { class_type: 'ModelSamplingSD3', inputs: { model: ['1', 0], shift: 8 } },
  }
  if (initImageName) {
    workflow['10'] = { class_type: 'LoadImage', inputs: { image: initImageName } }
    workflow['12'] = {
      class_type: 'WanImageToVideo',
      inputs: {
        positive: ['6', 0],
        negative: ['7', 0],
        vae: ['3', 0],
        width,
        height,
        length,
        batch_size: 1,
        start_image: ['10', 0],
      },
    }
  } else {
    workflow['12'] = {
      class_type: 'EmptyHunyuanLatentVideo',
      inputs: { width, height, length, batch_size: 1 },
    }
  }
  if (opts.lowNoise) {
    // Wan2.2 官方双模型链路:高噪 UNet 采样前半程,低噪接续后半程
    workflow['15'] = {
      class_type: 'UNETLoader',
      inputs: { unet_name: opts.lowNoise, weight_dtype: 'default' },
    }
    workflow['16'] = { class_type: 'ModelSamplingSD3', inputs: { model: ['15', 0], shift: 8 } }
    workflow['17'] = {
      class_type: 'BasicScheduler',
      inputs: { model: ['14', 0], scheduler: p.scheduler, steps: p.steps, denoise: 1 },
    }
    workflow['18'] = {
      class_type: 'SplitSigmas',
      inputs: { sigmas: ['17', 0], step: Math.max(1, Math.ceil(p.steps / 2)) },
    }
    workflow['19'] = { class_type: 'KSamplerSelect', inputs: { sampler_name: p.sampler } }
    workflow['20'] = {
      class_type: 'SamplerCustom',
      inputs: {
        model: ['14', 0],
        add_noise: true,
        noise_seed: seed,
        cfg: p.cfgScale,
        positive: [condSource.positive[0], condSource.positive[1]],
        negative: [condSource.negative[0], condSource.negative[1]],
        sampler: ['19', 0],
        sigmas: ['18', 0],
        latent_image: ['12', initImageName ? 2 : 0],
      },
    }
    workflow['21'] = {
      class_type: 'SamplerCustom',
      inputs: {
        model: ['16', 0],
        add_noise: false,
        noise_seed: 0,
        cfg: p.cfgScale,
        positive: [condSource.positive[0], condSource.positive[1]],
        negative: [condSource.negative[0], condSource.negative[1]],
        sampler: ['19', 0],
        sigmas: ['18', 1],
        latent_image: ['20', 0],
      },
    }
    workflow['23'] = { class_type: 'VAEDecode', inputs: { samples: ['21', 0], vae: ['3', 0] } }
  } else {
    workflow['22'] = {
      class_type: 'KSampler',
      inputs: {
        seed,
        steps: p.steps,
        cfg: p.cfgScale,
        sampler_name: p.sampler,
        scheduler: p.scheduler,
        denoise: 1,
        model: ['14', 0],
        positive: [condSource.positive[0], condSource.positive[1]],
        negative: [condSource.negative[0], condSource.negative[1]],
        latent_image: ['12', initImageName ? 2 : 0],
      },
    }
    workflow['23'] = { class_type: 'VAEDecode', inputs: { samples: ['22', 0], vae: ['3', 0] } }
  }
  appendVideoOutput(workflow, '23', p.fps)
  return workflow
}

/** 汇总一次执行的产物文件;SaveVideo 的输出键在不同版本可能是 images 或 videos,两者都收。 */
export function collectOutputFiles(entry: ComfyHistoryEntry): ComfyOutputFile[] {
  return Object.values(entry.outputs ?? {}).flatMap((o) => [
    ...(o.images ?? []),
    ...(o.videos ?? []),
  ])
}

/** 优先返回真正的视频文件,其次动画格式,最后兜底第一个产物。 */
export function pickVideoFile(files: ComfyOutputFile[]): ComfyOutputFile | undefined {
  return (
    files.find((f) => /\.(mp4|webm|mkv|gif)$/i.test(f.filename)) ??
    files.find((f) => /\.(webp|apng)$/i.test(f.filename)) ??
    files[0]
  )
}

function extOf(filename: string): string | undefined {
  const m = /\.([A-Za-z0-9]+)$/.exec(filename)
  return m ? m[1].toLowerCase() : undefined
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))
