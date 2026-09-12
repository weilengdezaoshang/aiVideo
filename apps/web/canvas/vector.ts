// 矢量化引擎封装(PRD §5.6 / F9,线框 S12)。
// 引擎:imagetracerjs v1.2.6(Unlicense,纯 JS,经经典脚本挂到 window.ImageTracer)。
// 为什么不用 PRD 初版的 vtracer:vtracer-webapp@0.4.0 的 wasm 聚类在多种参数×图片
// 组合下确定性 panic(wasm `unreachable`,上游 visioncortex 0.9 的 bug,与加载方式
// 无关,已实测复现),详见 docs/开发计划-GenCanvas.md §3 的实施修正。
// imagetracerjs 是同步转换:预览源由调用方下采样(≤1024px),单次通常 <300ms;
// 取消(abort)只在任务开始前生效,同步执行期无法打断。

export type VectorParams = { colors: number; detail: number; simplify: number }

/** 界面默认值(S12 滑杆的中位档) */
export function defaultVectorParams(): VectorParams {
  return { colors: 12, detail: 6, simplify: 8 }
}

/** 引擎是否就绪(经典脚本同步加载,页面就绪即可用) */
export function vectorEngineReady() {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- ImageTracer 经典脚本挂 window,无类型来源
  return typeof window !== 'undefined' && Boolean((window as any).ImageTracer)
}

/** 串行队列:前一个任务失败不阻塞后续排队 */
let chain = Promise.resolve()

export type VectorizeOptions = {
  image: HTMLImageElement | HTMLCanvasElement
  params?: Partial<VectorParams>
  onProgress?: (progress: number) => void
  signal?: AbortSignal
}

/** 矢量化一张图,返回 SVG 文本。并发调用自动排队。 */
export function vectorize({
  image,
  params = {},
  onProgress,
  signal,
}: VectorizeOptions): Promise<string> {
  const task = chain.then(() => runVectorize({ image, params, onProgress, signal }))
  chain = task.then(
    () => {},
    () => {},
  )
  return task
}

async function runVectorize({
  image,
  params,
  onProgress,
  signal,
}: VectorizeOptions): Promise<string> {
  if (signal?.aborted) {
    throw new DOMException('已取消', 'AbortError')
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- ImageTracer 经典脚本挂 window,无类型来源
  const tracer = (window as any).ImageTracer
  if (!tracer) {
    throw new Error('矢量引擎尚未加载完成,请稍后重试')
  }
  const merged = { ...defaultVectorParams(), ...params }
  // 防御性上限:超 1024px 的源等比缩小;矢量化对源分辨率不敏感(SVG 可任意缩放),
  // 降采样同时决定了转换耗时(同步执行)
  const maxSize = 1024
  const naturalW = (image as HTMLImageElement).naturalWidth || image.width
  const naturalH = (image as HTMLImageElement).naturalHeight || image.height
  const scale = Math.min(1, maxSize / Math.max(naturalW, naturalH))
  const canvas = document.createElement('canvas')
  canvas.width = Math.max(1, Math.round(naturalW * scale))
  canvas.height = Math.max(1, Math.round(naturalH * scale))
  const ctx = canvas.getContext('2d') as CanvasRenderingContext2D
  ctx.drawImage(image, 0, 0, canvas.width, canvas.height)
  const imagedata = ctx.getImageData(0, 0, canvas.width, canvas.height)

  // 界面语汇 → imagetracerjs 选项:
  //   colors   → numberofcolors(调色板大小)
  //   detail   → ltres/qtres(阈值越低越贴近原细节),0..10 → 1.8..0.2
  //   simplify → pathomit(短路径剔除,越大越简),0..64
  const detail = Math.min(10, Math.max(0, merged.detail))
  const options = {
    numberofcolors: Math.min(64, Math.max(2, Math.round(merged.colors))),
    ltres: 1.8 - 0.16 * detail,
    qtres: 1.8 - 0.16 * detail,
    pathomit: Math.min(4096, Math.max(0, Math.round(merged.simplify))),
    roundcoords: 2,
    viewbox: true,
  }
  onProgress?.(0.5)
  const svg: string = tracer.imagedataToSVG(imagedata, options)
  onProgress?.(1)
  return svg
}
