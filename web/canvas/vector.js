// vtracer 浏览器端矢量化封装(PRD §5.6 / F9,线框 S12)。
// 上游 vtracer-webapp@0.4.0 的官方入口(vtracer_webapp.js)是 wasm-bindgen "bundler"
// 目标 —— `import * as wasm from './vtracer_webapp_bg.wasm'` 只有打包器能解析,
// 纯浏览器 ESM 加载不了。本模块替代该入口:用 glue 导出的 wbg 函数表手动实例化
// wasm 并回注(set_wasm + __wbindgen_start),维持仓库零构建约定。
// 转换过程:rAF 分片 tick,不卡 UI;进度回调驱动预览;同一时刻只跑一个转换。

/** @typedef {{ mode: string, hierarchical: string, filterSpeckle: number, colorPrecision: number, layerDifference: number, cornerThreshold: number, lengthThreshold: number, maxIterations: number, spliceThreshold: number, pathPrecision: number }} VectorParams */

/** vtracer 官方 webapp 的默认参数(visioncortex GUI 同款) @returns {VectorParams} */
export function defaultVectorParams() {
  return {
    mode: 'spline',
    hierarchical: 'stacked',
    filterSpeckle: 4,
    colorPrecision: 6,
    layerDifference: 16,
    cornerThreshold: 60,
    lengthThreshold: 4.0,
    maxIterations: 10,
    spliceThreshold: 45,
    pathPrecision: 8,
  }
}

/** @type {any} glue 模块(vendor/vtracer/vtracer_webapp_bg.js) */
let glue = null
/** @type {Promise<any> | null} */
let loadPromise = null

/** 加载并初始化 wasm(幂等);失败时抛错供调用方提示 */
export function loadVtracer() {
  loadPromise ??= (async () => {
    glue = await import('../vendor/vtracer/vtracer_webapp_bg.js')
    const wasmUrl = new URL('../vendor/vtracer/vtracer_webapp_bg.wasm', import.meta.url)
    /** @type {any} */
    const imports = { './vtracer_webapp_bg.js': glue }
    let instance
    try {
      instance = (await WebAssembly.instantiateStreaming(fetch(wasmUrl), imports)).instance
    } catch {
      // instantiateStreaming 要求响应头 application/wasm;不满足时退回字节实例化
      const bytes = await (await fetch(wasmUrl)).arrayBuffer()
      instance = (await WebAssembly.instantiate(bytes, imports)).instance
    }
    /** @type {any} wasm 实例导出由运行时 wasm 二进制决定,静态类型不完整 */
    const exports = instance.exports
    glue.__wbg_set_wasm(exports)
    exports.__wbindgen_start()
    return glue
  })()
  return loadPromise
}

/** JSON 参数转 vtracer Rust 侧的 snake_case 字段 @param {VectorParams} params */
function toRustParams(params) {
  return {
    mode: params.mode,
    hierarchical: params.hierarchical,
    filter_speckle: params.filterSpeckle,
    color_precision: params.colorPrecision,
    layer_difference: params.layerDifference,
    corner_threshold: params.cornerThreshold,
    length_threshold: params.lengthThreshold,
    max_iterations: params.maxIterations,
    splice_threshold: params.spliceThreshold,
    path_precision: params.pathPrecision,
  }
}

let seq = 0
/** 串行队列:前一个任务失败不阻塞后续排队 */
let chain = Promise.resolve()

/**
 * 矢量化一张图,返回 SVG 文本。并发调用自动排队(预览滑杆频繁变更时
 * 调用方应传入 signal 丢弃过期任务)。
 * @param {{
 *   image: HTMLImageElement | HTMLCanvasElement,
 *   params?: Partial<VectorParams>,
 *   onProgress?: (progress: number) => void,
 *   signal?: AbortSignal,
 * }} options
 * @returns {Promise<string>} SVG 文本
 */
export function vectorize({ image, params = {}, onProgress, signal }) {
  const task = chain.then(() => runVectorize({ image, params, onProgress, signal }))
  chain = task.then(
    () => {},
    () => {},
  )
  return task
}

/**
 * @param {{
 *   image: HTMLImageElement | HTMLCanvasElement,
 *   params: Partial<VectorParams>,
 *   onProgress?: (progress: number) => void,
 *   signal?: AbortSignal,
 * }} options
 */
async function runVectorize({ image, params, onProgress, signal }) {
  if (signal?.aborted) {
    throw new DOMException('已取消', 'AbortError')
  }
  await loadVtracer()
  const merged = { ...defaultVectorParams(), ...params }
  seq += 1
  const canvasId = `vtracer-src-${seq}`
  const host = document.createElement('div')
  // hidden 属性即可:getImageData 不依赖元素可见性
  host.hidden = true
  const canvas = document.createElement('canvas')
  canvas.id = canvasId
  // 防御性上限:超过 4096px 的源等比缩小,避免像素读取阶段内存爆炸
  const maxSize = 4096
  const naturalW = /** @type {any} */ (image).naturalWidth || image.width
  const naturalH = /** @type {any} */ (image).naturalHeight || image.height
  const scale = Math.min(1, maxSize / Math.max(naturalW, naturalH))
  canvas.width = Math.max(1, Math.round(naturalW * scale))
  canvas.height = Math.max(1, Math.round(naturalH * scale))
  const ctx = /** @type {CanvasRenderingContext2D} */ (canvas.getContext('2d'))
  ctx.drawImage(image, 0, 0, canvas.width, canvas.height)
  host.append(canvas)
  document.body.append(host)
  try {
    const converter = glue.ColorImageConverter.new_with_string(
      JSON.stringify({
        canvas_id: canvasId,
        svg_id: `${canvasId}-out`,
        ...toRustParams(merged),
      }),
    )
    converter.init()
    // rAF 分片:tick 返回 false 表示转换结束
    /** @type {Promise<void>} */
    const done = new Promise((resolve, reject) => {
      const step = () => {
        if (signal?.aborted) {
          converter.free()
          reject(new DOMException('已取消', 'AbortError'))
          return
        }
        const finished = !converter.tick()
        onProgress?.(converter.progress())
        if (finished) {
          resolve()
        } else {
          requestAnimationFrame(step)
        }
      }
      requestAnimationFrame(step)
    })
    await done
    const svgEl = /** @type {SVGElement | null} */ (host.querySelector('svg'))
    const result = svgEl ? svgEl.outerHTML : ''
    converter.free()
    return result
  } finally {
    host.remove()
  }
}
