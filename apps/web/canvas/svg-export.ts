// SVG 导出调参面板(PRD §5.6 / F9,线框 S12)。
// 核心 API 在 vector.ts(vectorize);本文件只做 UI:
// 原图 ↔ 实时预览、三滑杆(颜色层数/细节程度/简化程度)、导出下载。
// 入口由对象右键菜单(T9)接线:openSvgExportDialog({ imageUrl, filename })。

import { defaultVectorParams, vectorize } from './vector.js'

const $ = (sel: string) => document.querySelector<HTMLElement>(sel)!

/** 滑杆 → 矢量化参数的映射(界面语汇,vector.ts 负责换算引擎选项) */
const SLIDERS: {
  id: string
  label: string
  min: number
  max: number
  step: number
  value: () => number
  hint: (v: number) => string
}[] = [
  {
    id: 'svg-colors',
    label: '颜色层数',
    min: 2,
    max: 16,
    step: 1,
    /** 层数越多颜色越丰富 */
    value: () => defaultVectorParams().colors,
    hint: (v) => `${v} 层`,
  },
  {
    id: 'svg-detail',
    label: '细节程度',
    min: 0,
    max: 10,
    step: 1,
    /** 细节高 = 路径拟合阈值低,更贴近原图 */
    value: () => defaultVectorParams().detail,
    hint: (v) => (v <= 3 ? '简洁' : v >= 8 ? '保留细节' : '均衡'),
  },
  {
    id: 'svg-simplify',
    label: '简化程度',
    min: 0,
    max: 64,
    step: 1,
    /** 简化 = 短路径剔除更狠(pathomit 大) */
    value: () => defaultVectorParams().simplify,
    hint: (v) => (v <= 4 ? '精细' : v >= 40 ? '极简' : '适中'),
  },
]

function ensureDialog() {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Shoelace 自定义元素,无类型来源
  const dialog = $('#svg-dialog') as any // Shoelace 自定义元素,无类型来源
  return {
    dialog,
    preview: $('#svg-preview'),
    spin: $('#svg-preview-spin'),
    image: document.querySelector<HTMLImageElement>('#svg-source')!,
  }
}

async function toast(message: string, variant = 'success') {
  // sl-alert 可能尚未升级:先等定义完成,否则 .toast() 不存在且会中断调用方
  await customElements.whenDefined('sl-alert')
  const alert = Object.assign(document.createElement('sl-alert'), {
    variant,
    closable: true,
    duration: 3000,
    innerHTML: `<sl-icon name="${variant === 'success' ? 'check2-circle' : 'exclamation-triangle'}" slot="icon"></sl-icon>${message}`,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Shoelace 自定义元素,无类型来源
  }) as any
  document.body.append(alert)
  alert.toast()
}

let wired = false
let currentTask: AbortController | null = null
let debounceTimer: ReturnType<typeof setTimeout> | null = null
let lastSvg = ''
let downloadName = '矢量图.svg'
/** 初始与滑杆显示同源(defaultVectorParams) */
const sliderValues = defaultVectorParams()

/** 用当前滑杆值跑一次矢量化并刷新预览 */
async function renderPreview() {
  const { preview, spin } = ensureDialog()
  currentTask?.abort()
  const task = new AbortController()
  currentTask = task
  spin.hidden = false
  try {
    await new Promise((r) => setTimeout(r, 30)) // 让 spinner 先画出来
    const svg = await vectorize({
      image: ensureDialog().image,
      params: { ...sliderValues },
      signal: task.signal,
    })
    if (task.signal.aborted) {
      return
    }
    lastSvg = svg
    preview.innerHTML = svg
    const svgEl = preview.querySelector('svg')
    if (svgEl) {
      svgEl.removeAttribute('width')
      svgEl.removeAttribute('height')
      svgEl.style.maxWidth = '100%'
      svgEl.style.maxHeight = '100%'
    }
  } catch (err) {
    if (!(err instanceof DOMException && err.name === 'AbortError')) {
      toast(`矢量化失败:${err instanceof Error ? err.message : String(err)}`, 'danger')
    }
  } finally {
    if (currentTask === task) {
      currentTask = null
      spin.hidden = true
    }
  }
}

function schedulePreview(immediate = false) {
  if (debounceTimer) {
    clearTimeout(debounceTimer)
  }
  debounceTimer = setTimeout(() => void renderPreview(), immediate ? 0 : 350)
}

function initSliderEvents() {
  for (const slider of SLIDERS) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Shoelace 自定义元素,无类型来源
    const el = $(`#${slider.id}`) as any as HTMLInputElement
    el.value = String(slider.value())
    $(`#${slider.id}-hint`).textContent = slider.hint(slider.value())
    el.addEventListener('sl-input', () => {
      const v = Number(el.value)
      sliderValues[
        slider.id === 'svg-colors' ? 'colors' : slider.id === 'svg-detail' ? 'detail' : 'simplify'
      ] = v
      $(`#${slider.id}-hint`).textContent = slider.hint(v)
      schedulePreview()
    })
  }
}

/**
 * 打开导出面板(S12)。T9 右键菜单接线:对图像对象传入其原图 URL。
 */
export async function openSvgExportDialog({
  imageUrl,
  filename,
}: {
  imageUrl: string
  filename?: string
}) {
  const { dialog, image, preview, spin } = ensureDialog()
  await customElements.whenDefined('sl-dialog')
  downloadName = filename || '矢量图.svg'
  lastSvg = ''
  preview.innerHTML = ''
  spin.hidden = false
  image.src = imageUrl
  if (!wired) {
    wired = true
    initSliderEvents()
    $('#svg-cancel').addEventListener('click', () => dialog.hide())
    $('#svg-export').addEventListener('click', () => {
      if (!lastSvg) {
        toast('预览还没生成好,稍等一下', 'warning')
        return
      }
      const blob = new Blob([lastSvg], { type: 'image/svg+xml' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = downloadName
      a.click()
      URL.revokeObjectURL(url)
      toast(`已导出 ${downloadName}`)
    })
    dialog.addEventListener('sl-hide', () => currentTask?.abort())
  }
  dialog.show()
  // 等图片解码完成后立即出第一版预览
  if (!image.complete) {
    await new Promise((resolve, reject) => {
      image.addEventListener('load', resolve, { once: true })
      image.addEventListener('error', () => reject(new Error('原图加载失败')), { once: true })
    })
  }
  schedulePreview(true)
}
