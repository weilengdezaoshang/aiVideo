import { randomUUID } from 'node:crypto'
import { logger } from '../../logger.js'
import type { BackendStatus, GenParams, InitImage, ModelInfo, SamplerOptions } from '../../types.js'
import type { GenContext, GeneratedImage, GenerationProvider } from './provider.js'

interface ComfyOutputImage {
  filename: string
  subfolder: string
  type: string
}

interface ComfyHistoryEntry {
  outputs?: Record<string, { images?: ComfyOutputImage[] }>
  status?: { status_str?: string }
}

type ComfyWorkflow = Record<string, { class_type: string; inputs: Record<string, unknown> }>

/** 真实出图后端:走 ComfyUI HTTP API(/prompt 提交工作流 + 轮询 /history + /view 取图)。 */
export class ComfyUIProvider implements GenerationProvider {
  readonly name = 'comfyui'
  readonly capacity = 1

  constructor(private baseUrl: string) {}

  private async fetchJson<T>(
    pathName: string,
    init: RequestInit & { timeoutMs?: number } = {},
  ): Promise<T> {
    const { timeoutMs = 5000, ...rest } = init
    const res = await fetch(`${this.baseUrl}${pathName}`, {
      ...rest,
      signal: rest.signal ?? AbortSignal.timeout(timeoutMs),
    })
    if (!res.ok) {
      throw new Error(`ComfyUI ${pathName} 返回 HTTP ${res.status}`)
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
    const info = await this.fetchJson<
      Record<string, { input?: { required?: { ckpt_name?: [string[]] } } }>
    >('/object_info/CheckpointLoaderSimple')
    const names = info.CheckpointLoaderSimple?.input?.required?.ckpt_name?.[0] ?? []
    return names.map((n) => ({ id: n, name: n }))
  }

  async listSamplerOptions(): Promise<SamplerOptions> {
    const info = await this.fetchJson<{
      KSampler?: { input?: { required?: { sampler_name?: [string[]]; scheduler?: [string[]] } } }
    }>('/object_info/KSampler')
    const required = info.KSampler?.input?.required
    return {
      samplers: required?.sampler_name?.[0] ?? ['euler'],
      schedulers: required?.scheduler?.[0] ?? ['normal'],
    }
  }

  async generateVideo(
    _params: GenParams,
    _ctx: GenContext,
    _initImage?: InitImage,
  ): Promise<GeneratedImage> {
    // 视频工作流(Wan2.2-I2V / LTX)依赖真实环境里的模型文件与节点版本,
    // 盲写无法验证只会留下返工,等阶段 1.5 在真实 ComfyUI 上校准后再启用。
    throw new Error('ComfyUI 视频后端待真实环境校准后启用,当前请用 Mock 后端演示视频流程')
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

    const startedAt = Date.now()
    const estimateMs = 15000 + params.steps * 600
    for (;;) {
      if (Date.now() - startedAt > 20 * 60 * 1000) {
        throw new Error('等待 ComfyUI 超时(20 分钟)')
      }
      if (ctx.signal.aborted) {
        await this.interrupt()
        throw new Error('已取消')
      }
      await sleep(700)
      const elapsed = Date.now() - startedAt
      ctx.onProgress(
        Math.min(0.9, 0.05 + (elapsed / estimateMs) * 0.85),
        `ComfyUI 采样中 ${Math.round(elapsed / 1000)}s`,
      )

      const history = await this.fetchJson<Record<string, ComfyHistoryEntry>>(
        `/history/${submit.prompt_id}`,
        { timeoutMs: 10000 },
      )
      const entry = history[submit.prompt_id]
      if (!entry) {
        continue
      }
      if (entry.status?.status_str === 'error') {
        throw new Error('ComfyUI 执行出错,请查看其日志')
      }
      const images = Object.values(entry.outputs ?? {}).flatMap((o) => o.images ?? [])
      if (images.length === 0) {
        continue
      }

      const out = images[0]
      const query = `filename=${encodeURIComponent(out.filename)}&subfolder=${encodeURIComponent(out.subfolder)}&type=${encodeURIComponent(out.type)}`
      const res = await fetch(`${this.baseUrl}/view?${query}`, {
        signal: AbortSignal.timeout(60000),
      })
      if (!res.ok) {
        throw new Error(`下载生成结果失败:HTTP ${res.status}`)
      }
      ctx.onProgress(1, '生成完成')
      logger.debug('ComfyUI 出图完成', {
        promptId: submit.prompt_id,
        elapsedMs: Date.now() - startedAt,
      })
      return { data: Buffer.from(await res.arrayBuffer()), ext: 'png' }
    }
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
    const res = await fetch(`${this.baseUrl}/upload/image`, {
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
      await fetch(`${this.baseUrl}/interrupt`, {
        method: 'POST',
        signal: AbortSignal.timeout(2000),
      })
    } catch (err) {
      // 尽力而为:ComfyUI 可能已经不在执行该任务
      logger.warn('中断 ComfyUI 任务失败', { err })
    }
  }
}

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

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))
