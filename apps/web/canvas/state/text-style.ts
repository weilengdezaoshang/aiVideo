import type { CanvasObj } from './commands.js'

export const TEXT_ALIGNS = ['left', 'center', 'right'] as const
export const TEXT_REGIONS = ['title', 'body', 'footer'] as const
export type TextAlign = (typeof TEXT_ALIGNS)[number]
export type TextRegion = (typeof TEXT_REGIONS)[number]

export type TextStyle = {
  text: string
  fontFamily: string
  fontSize: number
  fill: string
  textAlign: TextAlign
  region?: TextRegion
}

const COLOR = /^#[0-9A-Fa-f]{6}$/
const LINE_HEIGHT = 1.3

export function measureTextWidth(text: string, fontSize: number, fontFamily: string): number {
  if (typeof document !== 'undefined') {
    const canvas = document.createElement('canvas')
    const context = canvas.getContext('2d')
    if (context) {
      context.font = `${fontSize}px ${fontFamily}`
      const width = context.measureText(text).width
      if (width > 0) {
        return width
      }
    }
  }
  return [...text].length * fontSize
}

/** Wrap inside the text box. The joined lines keep the original characters, including spaces. */
export function layoutLineBoxes(
  object: Pick<CanvasObj, 'width'>,
  style: TextStyle,
  measure: (text: string, fontSize: number, fontFamily: string) => number = measureTextWidth,
): { text: string; x: number; y: number; width: number; height: number }[] {
  const limit = Math.max(1, object.width)
  const lines: string[] = []
  let current = ''
  for (const char of style.text) {
    if (char === '\n') {
      lines.push(current)
      current = ''
      continue
    }
    const trial = current + char
    if (current && measure(trial, style.fontSize, style.fontFamily) > limit) {
      lines.push(current)
      current = char
    } else {
      current = trial
    }
  }
  lines.push(current)
  const step = Math.max(1, Math.round(style.fontSize * LINE_HEIGHT))
  return lines.map((text, index) => {
    const width = text ? measure(text, style.fontSize, style.fontFamily) : 0
    const x =
      style.textAlign === 'right'
        ? Math.max(0, limit - width)
        : style.textAlign === 'center'
          ? Math.max(0, Math.floor((limit - width) / 2))
          : 0
    return { text, x, y: index * step, width, height: step }
  })
}

export function readTextStyle(object: CanvasObj): TextStyle {
  const align = object.textAlign
  const region = object.region
  return {
    text: object.text ?? '',
    fontFamily: object.fontFamily ?? 'noto-sans-sc',
    fontSize: object.fontSize ?? 16,
    fill: object.fill ?? '#202124',
    textAlign: align === 'center' || align === 'right' || align === 'left' ? align : 'left',
    region: region === 'title' || region === 'body' || region === 'footer' ? region : undefined,
  }
}

export function textStyleUpdate(
  object: CanvasObj,
  patch: Partial<TextStyle>,
): { patch: Record<string, unknown>; prev: Record<string, unknown> } | null {
  const current = readTextStyle(object)
  const next: TextStyle = { ...current, ...patch }
  if (!next.text || next.text.length > 4000) {
    return null
  }
  if (!Number.isInteger(next.fontSize) || next.fontSize < 1 || next.fontSize > 1024) {
    return null
  }
  if (!COLOR.test(next.fill) || !next.fontFamily || next.fontFamily.length > 128) {
    return null
  }
  if (!TEXT_ALIGNS.includes(next.textAlign)) {
    return null
  }
  if (next.region !== undefined && !TEXT_REGIONS.includes(next.region)) {
    return null
  }
  const keys = ['text', 'fontFamily', 'fontSize', 'fill', 'textAlign', 'region'] as const
  const changed: Record<string, unknown> = {}
  const prev: Record<string, unknown> = {}
  for (const key of keys) {
    if (next[key] !== current[key]) {
      changed[key] = next[key]
      prev[key] = object[key]
    }
  }
  if (
    'text' in changed ||
    'fontSize' in changed ||
    'textAlign' in changed ||
    'fontFamily' in changed
  ) {
    changed.lineBoxes = layoutLineBoxes(object, next)
    prev.lineBoxes = object.lineBoxes
  }
  if (Object.keys(changed).length === 0) {
    return null
  }
  return { patch: changed, prev }
}
