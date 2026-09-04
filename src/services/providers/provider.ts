import type { BackendStatus, GenParams, InitImage, ModelInfo, SamplerOptions } from '../../types.js'

/** 单张图片的生成上下文:种子已解析、进度回调与取消信号。 */
export interface GenContext {
  seed: number
  index: number
  onProgress: (progress: number, message: string) => void
  signal: AbortSignal
}

export interface GeneratedImage {
  data: Buffer
  ext: string
}

/** 生成后端抽象:Mock 与 ComfyUI 各实现一份,JobManager 只面向此接口。 */
export interface GenerationProvider {
  readonly name: string
  /** 同时处理的最大任务数 */
  readonly capacity: number
  status(): Promise<BackendStatus>
  listModels(): Promise<ModelInfo[]>
  listSamplerOptions(): Promise<SamplerOptions>
  /** initImage 存在且 denoise < 1 时按图生图处理。 */
  generate(params: GenParams, ctx: GenContext, initImage?: InitImage): Promise<GeneratedImage>
  /** 图生视频:initImage 作为首帧(未提供时按文生视频)。 */
  generateVideo(params: GenParams, ctx: GenContext, initImage?: InitImage): Promise<GeneratedImage>
}
