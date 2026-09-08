import type { BackendStatus, GenParams, InitImage, ModelInfo, SamplerOptions } from '../../types.js'
import type { GenContext, GeneratedImage, GenerationProvider } from './provider.js'

export type CloudVendorId = 'zhipu' | 'siliconflow' | 'openai'

/**
 * 云端生图厂商适配(PRD BYOK,§11-4 首批清单):
 *   zhipu       智谱 CogView-4(国内直连,POST {base}/images/generations → data[].url)
 *   siliconflow 硅基流动 FLUX 系列(POST {base}/images/generations → images[].url)
 *   openai      OpenAI 兼容(gpt-image / 任意自建网关,→ data[].b64_json|url)
 *
 * 设计原则(对应"减少幻觉与花费"):
 *   - 提示词直通:不经过任何 LLM 改写层,用户输入即请求体 prompt;
 *   - 零轮询计费:status() 不发网络请求(生图按张计费,健康检查不能花钱);
 *   - 云端模型不支持的字段(steps/cfg/sampler/negative)静默忽略,不假装生效。
 */
export interface CloudVendorProfile {
  id: CloudVendorId
  label: string
  defaultBaseUrl: string
  defaultModel: string
  /** 构造请求:返回完整 URL 与请求体 */
  request(args: { baseUrl: string; model: string; prompt: string; size: string }): {
    url: string
    body: Record<string, unknown>
  }
  /** 从响应体解析图像(厂商错误形状在此抛中文错误) */
  parse(body: unknown): { url?: string; b64?: string }
  /** 图生图(无蒙版,指令编辑);不支持的厂商不实现 */
  requestI2I?(args: { baseUrl: string; prompt: string; size: string; initImage: InitImage }): {
    url: string
    body: Record<string, unknown>
  }
  /** 蒙版重绘(白 = 重绘区域);不支持的厂商不实现 */
  requestInpaint?(args: {
    baseUrl: string
    prompt: string
    size: string
    initImage: InitImage
    maskImage: InitImage
  }): { url: string; body: Record<string, unknown> }
}

/** 是否包含中文(触发中译英的判定) */
export function hasChinese(text: string): boolean {
  return /[\u4e00-\u9fff]/.test(text)
}

export interface TranslateOptions {
  apiKey: string
  baseUrl: string
  model: string
  prompt: string
  signal: AbortSignal
  fetchImpl?: typeof fetch
}

/**
 * 提示词中译英(确定性翻译,temperature≈0,只输出译文):
 * 国外模型对英文跟随显著更好;翻译≠改写,不引入语义幻觉增量。
 * 网络失败向上抛,调用方降级回原文。
 */
export async function translateToEnglish(options: TranslateOptions): Promise<string> {
  const {
    apiKey,
    baseUrl,
    model,
    prompt,
    signal,
    fetchImpl = (...args) => fetch(...args),
  } = options
  const res = await fetchImpl(`${baseUrl}/chat/completions`, {
    method: 'POST',
    signal,
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify({
      model,
      temperature: 0.1,
      max_tokens: 500,
      messages: [
        {
          role: 'system',
          content:
            'You are a translator for image-generation prompts. Translate the user text to English literally. Keep style words, counts, positions and named entities. Output ONLY the translation, no explanations, no quotes.',
        },
        { role: 'user', content: prompt },
      ],
    }),
  })
  const body = (await res.json().catch(() => ({}))) as {
    choices?: Array<{ message?: { content?: string } }>
    error?: { message?: string }
    message?: string
  }
  if (!res.ok) {
    throw new Error(body?.error?.message || body?.message || `翻译失败(HTTP ${res.status})`)
  }
  const text = body?.choices?.[0]?.message?.content?.trim()
  if (!text) {
    throw new Error('翻译结果为空')
  }
  return text
}

/** InitImage → data URL(厂商 API 接受 base64 内联图) */
function toDataUrl(image: InitImage): string {
  const mime = image.ext === 'jpg' ? 'image/jpeg' : `image/${image.ext}`
  return `data:${mime};base64,${image.data.toString('base64')}`
}

function parseOpenAiLike(body: unknown): { url?: string; b64?: string } {
  const b = body as {
    data?: Array<{ url?: string; b64_json?: string }>
    error?: { message?: string }
    message?: string
  }
  const item = b?.data?.[0]
  if (!item) {
    throw new Error(b?.error?.message || b?.message || '云端响应中没有图像数据')
  }
  if (item.b64_json) {
    return { b64: item.b64_json }
  }
  if (item.url) {
    return { url: item.url }
  }
  throw new Error('云端响应缺少图像 url/b64')
}

export const CLOUD_VENDORS: Record<CloudVendorId, CloudVendorProfile> = {
  zhipu: {
    id: 'zhipu',
    label: '智谱 CogView',
    defaultBaseUrl: 'https://open.bigmodel.cn/api/paas/v4',
    defaultModel: 'cogview-4-250304',
    request: ({ baseUrl, model, prompt, size }) => ({
      url: `${baseUrl}/images/generations`,
      body: { model, prompt, size },
    }),
    parse: parseOpenAiLike,
  },
  siliconflow: {
    id: 'siliconflow',
    label: '硅基流动 FLUX',
    defaultBaseUrl: 'https://api.siliconflow.cn/v1',
    defaultModel: 'black-forest-labs/FLUX.1-schnell',
    request: ({ baseUrl, model, prompt, size }) => ({
      url: `${baseUrl}/images/generations`,
      body: { model, prompt, image_size: size, batch_size: 1 },
    }),
    parse: (body) => {
      const b = body as { images?: Array<{ url?: string }>; message?: string }
      if (b?.images?.[0]?.url) {
        return { url: b.images[0].url }
      }
      return parseOpenAiLike(body)
    },
    /** 图生图:Kontext(指令编辑,无蒙版);模型固定为托管 Kontext */
    requestI2I: ({ baseUrl, prompt, size, initImage }) => ({
      url: `${baseUrl}/images/generations`,
      body: {
        model: 'black-forest-labs/FLUX.1-Kontext-dev',
        prompt,
        image: toDataUrl(initImage),
        image_size: size,
        batch_size: 1,
      },
    }),
    /** 蒙版重绘:Fill-dev(白 = 重绘区域,与画布蒙版语义一致) */
    requestInpaint: ({ baseUrl, prompt, size, initImage, maskImage }) => ({
      url: `${baseUrl}/images/generations`,
      body: {
        model: 'black-forest-labs/FLUX.1-Fill-dev',
        prompt,
        image: toDataUrl(initImage),
        image_mask: toDataUrl(maskImage),
        image_size: size,
        batch_size: 1,
      },
    }),
  },
  openai: {
    id: 'openai',
    label: 'OpenAI 兼容',
    defaultBaseUrl: 'https://api.openai.com/v1',
    defaultModel: 'gpt-image-1',
    request: ({ baseUrl, model, prompt, size }) => ({
      url: `${baseUrl}/images/generations`,
      body: { model, prompt, size, n: 1 },
    }),
    parse: parseOpenAiLike,
  },
}

export const CLOUD_VENDOR_IDS = Object.keys(CLOUD_VENDORS) as CloudVendorId[]

export interface CloudProviderOptions {
  vendor: CloudVendorId
  baseUrl: string
  model: string
  apiKey: string
  /** 单次生成的整体超时(含排队与下载) */
  imageTimeoutMs: number
  fetchImpl?: typeof fetch
}

export class CloudImagesProvider implements GenerationProvider {
  readonly name = 'cloud'
  /** 云端并发保守取 2:多数厂商对单 Key 有限流,太高只会成批 429 */
  readonly capacity = 2
  private profile: CloudVendorProfile
  private fetchImpl: typeof fetch

  constructor(private opts: CloudProviderOptions) {
    this.profile = CLOUD_VENDORS[opts.vendor]
    this.fetchImpl = opts.fetchImpl ?? ((...args) => fetch(...args))
  }

  /** 不发网络请求(生图按张计费):健康度只反映 Key 配置状态 */
  async status(): Promise<BackendStatus> {
    if (!this.opts.apiKey) {
      return { ok: false, detail: '未配置图像 API Key(设置中填写后即可生成)' }
    }
    return { ok: true, detail: `${this.profile.label} · ${this.opts.model}(按张计费)` }
  }

  async listModels(): Promise<ModelInfo[]> {
    return [{ id: this.opts.model, name: `${this.profile.label} ${this.opts.model}` }]
  }

  /** 云端模型无采样器概念:返回占位项,参数面板的取值会被静默忽略 */
  async listSamplerOptions(): Promise<SamplerOptions> {
    return { samplers: ['euler'], schedulers: ['default'] }
  }

  /** 提示词直通文生图。i2i/蒙版:当前三家云 API 均不通用,显式报错交给画布错误卡 */
  async generate(
    params: GenParams,
    ctx: GenContext,
    initImage?: InitImage,
    maskImage?: InitImage,
  ): Promise<GeneratedImage> {
    const prompt = params.prompt.trim()
    if (!prompt) {
      throw new Error('提示词为空')
    }
    const signal = AbortSignal.any([ctx.signal, AbortSignal.timeout(this.opts.imageTimeoutMs)])
    const size = `${params.width}x${params.height}`
    let spec: { url: string; body: Record<string, unknown> }
    if (initImage && maskImage) {
      if (!this.profile.requestInpaint) {
        throw new Error(
          `当前厂商(${this.profile.label})暂不支持蒙版局部重绘,请使用硅基流动或本地后端`,
        )
      }
      ctx.onProgress(0.1, '局部重绘(FLUX.1-Fill)')
      spec = this.profile.requestInpaint({
        baseUrl: this.opts.baseUrl,
        prompt,
        size,
        initImage,
        maskImage,
      })
    } else if (initImage) {
      if (!this.profile.requestI2I) {
        throw new Error(`当前厂商(${this.profile.label})暂不支持参考图,请使用纯文生图`)
      }
      ctx.onProgress(0.1, '以图生图(FLUX.1-Kontext)')
      spec = this.profile.requestI2I({
        baseUrl: this.opts.baseUrl,
        prompt,
        size,
        initImage,
      })
    } else {
      ctx.onProgress(0.15, `请求 ${this.opts.model}`)
      spec = this.profile.request({
        baseUrl: this.opts.baseUrl,
        model: this.opts.model,
        prompt,
        size,
      })
    }
    const res = await this.fetchImpl(spec.url, {
      method: 'POST',
      signal,
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${this.opts.apiKey}`,
      },
      body: JSON.stringify(spec.body),
    })
    const raw = await res.text()
    let payload: unknown
    try {
      payload = JSON.parse(raw)
    } catch {
      throw new Error(`云端响应不是 JSON(HTTP ${res.status}):${raw.slice(0, 120)}`)
    }
    if (!res.ok) {
      const b = payload as { error?: { message?: string }; message?: string }
      throw new Error(b?.error?.message || b?.message || `云端生成失败(HTTP ${res.status})`)
    }
    const parsed = this.profile.parse(payload)
    ctx.onProgress(0.75, '下载图像')
    if (parsed.b64) {
      const data = Buffer.from(parsed.b64, 'base64')
      if (data.length === 0) {
        throw new Error('云端返回空图像')
      }
      ctx.onProgress(1, '完成')
      return { data, ext: 'png' }
    }
    const imageRes = await this.fetchImpl(parsed.url!, { signal })
    if (!imageRes.ok) {
      throw new Error(`图像下载失败(HTTP ${imageRes.status})`)
    }
    const data = Buffer.from(await imageRes.arrayBuffer())
    if (data.length === 0) {
      throw new Error('云端返回空图像')
    }
    const mime = imageRes.headers.get('content-type') || ''
    const ext = mime.includes('jpeg') ? 'jpg' : mime.includes('webp') ? 'webp' : 'png'
    ctx.onProgress(1, '完成')
    return { data, ext }
  }

  async generateVideo(): Promise<GeneratedImage> {
    throw new Error('云端视频生成暂未接入:请切换演示模式或 ComfyUI 后端')
  }
}
