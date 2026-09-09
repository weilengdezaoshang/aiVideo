import type { CutoutMaskResult, CutoutProvider } from './cutout-provider.js'

export interface RembgProviderOptions {
  serviceUrl: string
  model: string
  timeoutMs?: number
  fetchImpl?: typeof fetch
}

/** 调用独立 rembg 服务；Node 主进程不加载 Python/ONNX 依赖。 */
export class RembgCutoutProvider implements CutoutProvider {
  readonly name = 'rembg'
  private fetchImpl: typeof fetch

  constructor(private options: RembgProviderOptions) {
    this.fetchImpl = options.fetchImpl ?? ((...args) => fetch(...args))
  }

  async createMask(
    image: Buffer,
    contentType: string,
    signal?: AbortSignal,
  ): Promise<CutoutMaskResult> {
    const timeout = AbortSignal.timeout(this.options.timeoutMs ?? 120_000)
    const combined = signal ? AbortSignal.any([signal, timeout]) : timeout
    const base = this.options.serviceUrl.replace(/\/+$/, '')
    const url = new URL(`${base}/v1/masks`)
    url.searchParams.set('model', this.options.model)
    const response = await this.fetchImpl(url, {
      method: 'POST',
      signal: combined,
      headers: { 'Content-Type': contentType },
      body: image.buffer.slice(image.byteOffset, image.byteOffset + image.byteLength) as ArrayBuffer,
    })
    if (!response.ok) {
      const detail = (await response.text()).slice(0, 500)
      throw new Error(`主体识别服务失败(HTTP ${response.status})${detail ? `:${detail}` : ''}`)
    }
    const data = Buffer.from(await response.arrayBuffer())
    if (data.length === 0) {
      throw new Error('主体识别服务返回了空 Mask')
    }
    return {
      data,
      model: response.headers.get('x-cutout-model') || this.options.model,
    }
  }
}
