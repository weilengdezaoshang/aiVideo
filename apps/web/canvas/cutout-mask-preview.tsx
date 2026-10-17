import { useEffect, useRef, useState, type CSSProperties } from 'react'
import type { MaskStroke } from './core/types.js'
import type { CanvasObj } from './state/commands.js'
import { paintCutoutMask } from './flows/cutout-mask.js'

export function CutoutMaskPreview({
  source,
  baseUrl,
  edits,
  visible,
  style,
  onError,
}: {
  source: CanvasObj
  baseUrl: string | null
  edits: readonly MaskStroke[]
  visible: boolean
  style: CSSProperties
  onError: () => void
}) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const [base, setBase] = useState<{ url: string; image: HTMLImageElement } | null>(null)
  useEffect(() => {
    if (!baseUrl) {
      return
    }
    let current = true
    const image = new Image()
    image.onload = () => {
      if (current) {
        setBase({ url: baseUrl, image })
      }
    }
    image.onerror = () => {
      if (current) {
        onError()
      }
    }
    image.src = baseUrl
    return () => {
      current = false
    }
  }, [baseUrl, onError])
  useEffect(() => {
    const element = canvas.current
    const context = element?.getContext('2d')
    if (!element || !context) {
      return
    }
    // Cap the preview buffer; the submitted mask still uses the original resolution.
    const ratio = Math.min(1, 1200 / Math.max(source.width, source.height))
    element.width = Math.max(1, Math.round(source.width * ratio))
    element.height = Math.max(1, Math.round(source.height * ratio))
    paintCutoutMask(
      context,
      source,
      element.width,
      element.height,
      base?.url === baseUrl ? base.image : null,
      edits,
    )
    const pixels = context.getImageData(0, 0, element.width, element.height)
    for (let i = 0; i < pixels.data.length; i += 4) {
      const alpha = pixels.data[i]
      pixels.data[i] = 154
      pixels.data[i + 1] = 114
      pixels.data[i + 2] = 255
      pixels.data[i + 3] = Math.round(alpha * 0.46)
    }
    context.putImageData(pixels, 0, 0)
  }, [source, baseUrl, base, edits])
  return (
    <canvas
      ref={canvas}
      className="next-cutout-mask-preview"
      aria-hidden="true"
      style={{ ...style, visibility: visible ? 'visible' : 'hidden' }}
    />
  )
}
