import type { BackendStatus, GenParams, InitImage, ModelInfo, SamplerOptions } from '../../types.js'
import type { GenContext, GeneratedImage, GenerationProvider } from './provider.js'

/** 内置演示后端:不依赖 GPU / 外部服务,按步模拟采样进度,输出带提示词与参数水印的占位图。 */
export class MockProvider implements GenerationProvider {
  readonly name = 'mock'
  readonly capacity = 2

  constructor(private stepMs = Number(process.env.MOCK_STEP_MS ?? 45)) {}

  async status(): Promise<BackendStatus> {
    return { ok: true, detail: '内置演示后端(Mock),直接返回占位图' }
  }

  async listModels(): Promise<ModelInfo[]> {
    return [
      { id: 'mock-diffusion-xl', name: 'Mock Diffusion XL(内置演示)' },
      { id: 'mock-anime-v3', name: 'Mock Anime v3(内置演示)' },
      { id: 'mock-photo-real', name: 'Mock PhotoReal(内置演示)' },
    ]
  }

  async listSamplerOptions(): Promise<SamplerOptions> {
    return {
      samplers: ['euler', 'euler_ancestral', 'dpmpp_2m', 'dpmpp_2m_sde', 'ddim', 'uni_pc'],
      schedulers: ['normal', 'karras', 'exponential', 'sgm_uniform', 'beta'],
    }
  }

  async generate(
    params: GenParams,
    ctx: GenContext,
    initImage?: InitImage,
  ): Promise<GeneratedImage> {
    const isImg2img = Boolean(initImage) && params.denoise < 1
    for (let step = 1; step <= params.steps; step++) {
      if (ctx.signal.aborted) {
        throw new Error('已取消')
      }
      await sleep(this.stepMs)
      ctx.onProgress(step / params.steps, `演示采样 step ${step}/${params.steps}`)
    }
    return {
      data: Buffer.from(this.renderSvg(params, ctx, initImage, isImg2img), 'utf8'),
      ext: 'svg',
    }
  }

  private renderSvg(
    params: GenParams,
    ctx: GenContext,
    initImage: InitImage | undefined,
    isImg2img: boolean,
  ): string {
    const rng = mulberry32(hashSeed(ctx.seed, params))
    const hue = Math.floor(rng() * 360)
    const hue2 = (hue + 60 + Math.floor(rng() * 180)) % 360
    const min = Math.min(params.width, params.height)

    const blobs = Array.from({ length: 14 }, () => {
      const cx = (rng() * params.width).toFixed(1)
      const cy = (rng() * params.height).toFixed(1)
      const r = ((0.08 + rng() * 0.3) * min).toFixed(1)
      const h = Math.floor(rng() * 360)
      const o = (0.08 + rng() * 0.25).toFixed(2)
      return `<circle cx="${cx}" cy="${cy}" r="${r}" fill="hsl(${h},80%,60%)" opacity="${o}"/>`
    }).join('')
    const dots = Array.from({ length: 120 }, () => {
      const cx = (rng() * params.width).toFixed(0)
      const cy = (rng() * params.height).toFixed(0)
      const r = (0.5 + rng() * 2).toFixed(1)
      const o = (rng() * 0.35).toFixed(2)
      return `<circle cx="${cx}" cy="${cy}" r="${r}" fill="#fff" opacity="${o}"/>`
    }).join('')

    // 图生图:参考图以 (1 - denoise) 的透明度垫底,直观体现重绘幅度
    const refLayer = initImage
      ? `<image href="data:${mimeOf(initImage.ext)};base64,${initImage.data.toString('base64')}" width="100%" height="100%" preserveAspectRatio="xMidYMid slice" opacity="${(1 - params.denoise).toFixed(2)}"/>`
      : ''

    const lines = wrapText(params.prompt, Math.max(14, Math.floor(params.width / 22))).slice(0, 8)
    const fontSize = Math.max(16, Math.round(min / 28))
    const textY = params.height / 2 - ((lines.length - 1) * fontSize * 1.4) / 2
    const mode = isImg2img ? ` · img2img ${params.denoise.toFixed(2)}` : ''
    const caption = `seed ${ctx.seed} · ${params.width}×${params.height} · steps ${params.steps} · cfg ${params.cfgScale} · ${params.sampler}/${params.scheduler}${mode} · ${escapeXml(params.model)}`

    return `<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="${params.width}" height="${params.height}" viewBox="0 0 ${params.width} ${params.height}">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="hsl(${hue},70%,22%)"/>
      <stop offset="1" stop-color="hsl(${hue2},65%,38%)"/>
    </linearGradient>
  </defs>
  <rect width="100%" height="100%" fill="url(#bg)"/>
  ${refLayer}
  ${blobs}
  ${dots}
  ${lines
    .map(
      (line, i) =>
        `<text x="50%" y="${(textY + i * fontSize * 1.4).toFixed(1)}" text-anchor="middle" font-family="PingFang SC, Microsoft YaHei, sans-serif" font-size="${fontSize}" fill="#ffffff" opacity="0.92">${escapeXml(line)}</text>`,
    )
    .join('\n  ')}
  <text x="50%" y="${params.height - fontSize * 1.8}" text-anchor="middle" font-family="monospace" font-size="${Math.max(11, Math.round(fontSize * 0.55))}" fill="#ffffff" opacity="0.6">${caption}</text>
  <text x="${params.width - 12}" y="30" text-anchor="end" font-family="monospace" font-size="${Math.max(12, Math.round(fontSize * 0.6))}" fill="#ffffff" opacity="0.35">MOCK</text>
</svg>`
  }
}

function mimeOf(ext: string): string {
  if (ext === 'png') {
    return 'image/png'
  }
  if (ext === 'webp') {
    return 'image/webp'
  }
  return 'image/jpeg'
}

function hashSeed(seed: number, params: GenParams): number {
  let h = (seed ^ 0x9e3779b9) >>> 0
  h = Math.imul(h, 16777619) ^ params.width
  h = Math.imul(h, 16777619) ^ params.height
  h = Math.imul(h, 16777619) ^ params.steps
  h = Math.imul(h, 16777619) ^ [...params.sampler].reduce((a, c) => a + c.charCodeAt(0), 0)
  return h >>> 0
}

function mulberry32(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) | 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function escapeXml(text: string): string {
  return text.replace(
    /[<>&'"]/g,
    (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', "'": '&apos;', '"': '&quot;' })[c]!,
  )
}

/** 中英混排折行:优先按词折,超长词(如中文长句)按显示宽度硬切。 */
function wrapText(text: string, maxChars: number): string[] {
  const lines: string[] = []
  for (const paragraph of text.split(/\n+/)) {
    let cur = ''
    for (const word of paragraph.split(/\s+/).filter(Boolean)) {
      let rest = word
      while (visualLength(cur) + visualLength(rest) > maxChars) {
        if (cur) {
          lines.push(cur)
          cur = ''
        }
        if (visualLength(rest) <= maxChars) {
          break
        }
        let chunk = ''
        for (const ch of rest) {
          if (visualLength(chunk) + visualLength(ch) > maxChars) {
            break
          }
          chunk += ch
        }
        lines.push(chunk)
        rest = rest.slice(chunk.length)
      }
      if (rest) {
        cur = cur ? `${cur} ${rest}` : rest
      }
    }
    if (cur) {
      lines.push(cur)
    }
  }
  return lines.length ? lines : ['']
}

function visualLength(text: string): number {
  let n = 0
  for (const ch of text) {
    n += ch.charCodeAt(0) > 0x2e80 ? 2 : 1
  }
  return n
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))
