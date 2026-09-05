/** 一次生成请求的完整参数(经服务端校验与规范化)。 */
export interface GenParams {
  prompt: string
  negativePrompt: string
  model: string
  /** 8 的倍数,64..2048 */
  width: number
  height: number
  /** 1..150 */
  steps: number
  /** 1..30 */
  cfgScale: number
  /** -1 表示由服务端随机分配基准种子 */
  seed: number
  /** 本任务生成的图片数量 1..16,种子按基准 + index 递增 */
  batchCount: number
  sampler: string
  scheduler: string
  /** 1 = 纯文生图;小于 1 且携带参考图时为图生图重绘幅度 */
  denoise: number
  /** 生成媒体类型;video 时 denoise/steps 等图像参数不生效 */
  kind: 'image' | 'video'
  /** 视频时长(秒),1..12 */
  durationSec: number
  /** 视频帧率,4..30 */
  fps: number
}

/** 图生图的参考图(仅在内存中传递,不进入历史记录)。 */
export interface InitImage {
  data: Buffer
  /** png | jpeg | webp */
  ext: string
}

export interface SamplerOptions {
  samplers: string[]
  schedulers: string[]
}

export interface ModelInfo {
  id: string
  name: string
}

export interface BackendStatus {
  ok: boolean
  detail: string
}

export interface ImageRecord {
  id: string
  jobId: string
  file: string
  url: string
  provider: string
  params: GenParams
  createdAt: string
  /** 用户收藏标记,持久化保存;收藏记录不参与历史裁剪 */
  starred?: boolean
}

export type JobStatus = 'queued' | 'running' | 'completed' | 'failed'

export interface Job {
  id: string
  status: JobStatus
  params: GenParams
  batchCount: number
  /** 是否携带图生图参考图 */
  hasInitImage: boolean
  /** 0..1,按 (已完成张数 + 当前张进度) / 总张数 计算 */
  progress: number
  message: string
  images: ImageRecord[]
  error?: string
  createdAt: string
  startedAt?: string
  finishedAt?: string
}
