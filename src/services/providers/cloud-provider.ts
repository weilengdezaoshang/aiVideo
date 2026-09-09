import type { BackendStatus, GenParams, InitImage, ModelInfo, SamplerOptions } from '../../types.js'
import type { GenContext, GeneratedImage, GenerationProvider } from './provider.js'

export type CloudVendorId = 'zhipu' | 'siliconflow' | 'openai' | 'aliyun'

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
  request(args: {
    baseUrl: string
    model: string
    prompt: string
    size: string
    negativePrompt?: string
    seed?: number
  }): {
    url: string
    body: Record<string, unknown>
  }
  /** 从响应体解析图像(厂商错误形状在此抛中文错误) */
  parse(body: unknown): { url?: string; b64?: string }
  /** 图生图(无蒙版,指令编辑);不支持的厂商不实现 */
  requestI2I?(args: {
    baseUrl: string
    model: string
    prompt: string
    size: string
    negativePrompt?: string
    seed?: number
    initImage: InitImage
  }): {
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

/** 当前默认文生图模型的参考图编辑使用经实测的 Edit 模型；显式配置的其他模型保持原样。 */
export function resolveCloudImageModel(
  vendor: CloudVendorId,
  model: string,
  hasReference: boolean,
): string {
  return vendor === 'aliyun' && model === 'qwen-image-2.0' && hasReference
    ? 'qwen-image-edit-plus'
    : model
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
  aliyun: {
    id: 'aliyun',
    label: '阿里云百炼',
    defaultBaseUrl: 'https://dashscope.aliyuncs.com/api/v1',
    defaultModel: 'qwen-image-2.0',
    request: ({ baseUrl, model, prompt, size, negativePrompt, seed }) => {
      const [width, height] = size.split('x').map(Number)
      if (!width || !height || width * height < 512 ** 2 || width * height > 2048 ** 2) {
        throw new Error('百炼文生图尺寸总像素需在 512×512 至 2048×2048 之间,建议先用 1024×1024')
      }
      const base = baseUrl.replace(/\/+$/, '')
      const endpoint = '/services/aigc/multimodal-generation/generation'
      return {
        url: base.endsWith(endpoint)
          ? base
          : `${base.endsWith('/api/v1') ? base : `${base}/api/v1`}${endpoint}`,
        body: {
          model,
          input: { messages: [{ role: 'user', content: [{ text: prompt }] }] },
          parameters: {
            size: `${width}*${height}`,
            n: 1,
            prompt_extend: false,
            watermark: false,
            negative_prompt: negativePrompt || '',
            ...(seed !== undefined && seed >= 0 && seed <= 2147483647 ? { seed } : {}),
          },
        },
      }
    },
    parse: (body) => {
      const b = body as {
        code?: string
        message?: string
        output?: { choices?: Array<{ message?: { content?: Array<{ image?: string }> } }> }
      }
      if (b?.code) {
        throw new Error(`百炼 ${b.code}: ${b.message || '生成失败'}`)
      }
      const url = b?.output?.choices
        ?.flatMap((c) => c.message?.content ?? [])
        .find((c) => typeof c.image === 'string' && c.image)?.image
      if (!url) {
        throw new Error(b?.message || '百炼响应中没有生成图片,请检查模型是否支持同步文生图')
      }
      return { url }
    },
    requestI2I: ({ baseUrl, model, prompt, size, negativePrompt, seed, initImage }) => {
      const [width, height] = size.split('x').map(Number)
      if (!width || !height || width * height < 512 ** 2 || width * height > 2048 ** 2) {
        throw new Error('百炼图像编辑尺寸总像素需在 512×512 至 2048×2048 之间')
      }
      const base = baseUrl.replace(/\/+$/, '')
      const endpoint = '/services/aigc/multimodal-generation/generation'
      return {
        url: base.endsWith(endpoint)
          ? base
          : `${base.endsWith('/api/v1') ? base : `${base}/api/v1`}${endpoint}`,
        body: {
          model,
          input: {
            messages: [
              {
                role: 'user',
                content: [{ image: toDataUrl(initImage) }, { text: prompt }],
              },
            ],
          },
          parameters: {
            size: `${width}*${height}`,
            n: 1,
            prompt_extend: false,
            watermark: false,
            negative_prompt: negativePrompt || '',
            ...(seed !== undefined && seed >= 0 && seed <= 2147483647 ? { seed } : {}),
          },
        },
      }
    },
  },
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
    return {
      ok: true,
      detail: `${this.profile.label} · ${this.opts.model}:已配置 Key,未验证鉴权或额度;请生成 1 张图片验证(按张计费)`,
    }
  }

  async listModels(): Promise<ModelInfo[]> {
    return [{ id: this.opts.model, name: `${this.profile.label} ${this.opts.model}` }]
  }

  /** 云端模型无采样器概念:返回占位项,参数面板的取值会被静默忽略 */
  async listSamplerOptions(): Promise<SamplerOptions> {
    return { samplers: ['euler'], schedulers: ['default'] }
  }

  /** 提示词直通文生图；图生图与蒙版按厂商能力选择对应协议。 */
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
    if (!this.opts.apiKey) {
      throw new Error('未配置图像 API Key,请先完成本机配置')
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
      const editModel = resolveCloudImageModel(this.opts.vendor, this.opts.model, true)
      ctx.onProgress(0.1, `以图生图(${this.profile.label} · ${editModel})`)
      spec = this.profile.requestI2I({
        baseUrl: this.opts.baseUrl,
        model: editModel,
        prompt,
        size,
        negativePrompt: params.negativePrompt,
        seed: ctx.seed,
        initImage,
      })
    } else {
      ctx.onProgress(0.15, `请求 ${this.opts.model}`)
      spec = this.profile.request({
        baseUrl: this.opts.baseUrl,
        model: this.opts.model,
        prompt,
        size,
        negativePrompt: params.negativePrompt,
        seed: ctx.seed,
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
      throw new Error(`云端响应不是 JSON(HTTP ${res.status}),请检查 API 地址`)
    }
    if (!res.ok) {
      const b = payload as { error?: { message?: string }; message?: string; code?: string }
      const detail = b?.error?.message || b?.message || '云端生成失败'
      throw new Error(
        `HTTP ${res.status}${b?.code ? ` ${b.code}` : ''}: ${detail}`.replaceAll(
          this.opts.apiKey,
          '[REDACTED]',
        ),
      )
    }
    let parsed: { url?: string; b64?: string }
    try {
      parsed = this.profile.parse(payload)
    } catch (err) {
      throw new Error((err as Error).message.replaceAll(this.opts.apiKey, '[REDACTED]'))
    }
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
