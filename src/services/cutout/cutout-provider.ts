export interface CutoutMaskResult {
  data: Buffer
  model: string
}

/** 主体分割统一接口：所有实现必须返回与原图同尺寸的灰度 PNG Mask。 */
export interface CutoutProvider {
  readonly name: string
  createMask(image: Buffer, contentType: string, signal?: AbortSignal): Promise<CutoutMaskResult>
}
