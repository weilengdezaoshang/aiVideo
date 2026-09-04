import type { GenParams, InitImage } from './types.js'

const clampNum = (value: unknown, min: number, max: number, fallback: number): number => {
  const n = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(n)) {
    return fallback
  }
  return Math.min(max, Math.max(min, n))
}

/** 校验并规范化生成参数;不合法时返回可读错误信息。 */
export function parseGenParams(
  raw: unknown,
): { ok: true; params: GenParams } | { ok: false; error: string } {
  if (typeof raw !== 'object' || raw === null) {
    return { ok: false, error: '请求体必须是 JSON 对象' }
  }
  const r = raw as Record<string, unknown>
  const prompt = typeof r.prompt === 'string' ? r.prompt.trim() : ''
  if (!prompt) {
    return { ok: false, error: '提示词(prompt)不能为空' }
  }
  if (prompt.length > 4000) {
    return { ok: false, error: '提示词过长(上限 4000 字符)' }
  }
  const model = typeof r.model === 'string' ? r.model.trim() : ''
  if (!model) {
    return { ok: false, error: '缺少模型(model)' }
  }

  const roundTo8 = (v: number) => Math.round(v / 8) * 8
  const seed = Math.floor(clampNum(r.seed, -1, 2 ** 31 - 1, -1))
  const cleanName = (v: unknown, fallback: string) => {
    const s = typeof v === 'string' ? v.trim() : ''
    return s && s.length <= 100 ? s : fallback
  }
  const params: GenParams = {
    prompt,
    negativePrompt:
      typeof r.negativePrompt === 'string' ? r.negativePrompt.trim().slice(0, 4000) : '',
    model,
    width: roundTo8(clampNum(r.width, 64, 2048, 512)),
    height: roundTo8(clampNum(r.height, 64, 2048, 512)),
    steps: Math.round(clampNum(r.steps, 1, 150, 20)),
    cfgScale: clampNum(r.cfgScale, 1, 30, 7),
    seed: seed < -1 ? -1 : seed,
    batchCount: Math.round(clampNum(r.batchCount, 1, 16, 1)),
    sampler: cleanName(r.sampler, 'euler'),
    scheduler: cleanName(r.scheduler, 'normal'),
    denoise: clampNum(r.denoise, 0.05, 1, 1),
  }
  return { ok: true, params }
}

const INIT_MAX_BYTES = 8 * 1024 * 1024

/**
 * 解析图生图参考图。接收 data URL(data:image/png|jpeg|webp;base64,...)。
 * image 为 undefined 表示请求未携带参考图。
 */
export function parseInitImage(
  raw: unknown,
): { ok: true; image: InitImage | undefined } | { ok: false; error: string } {
  if (raw === undefined || raw === null || raw === '') {
    return { ok: true, image: undefined }
  }
  if (typeof raw !== 'string') {
    return { ok: false, error: '参考图(initImage)必须是 data URL 字符串' }
  }
  const match = /^data:image\/(png|jpe?g|webp);base64,([A-Za-z0-9+/=\s]+)$/.exec(raw)
  if (!match) {
    return { ok: false, error: '参考图格式错误:仅支持 PNG / JPEG / WebP 的 base64 data URL' }
  }
  const data = Buffer.from(match[2].replace(/\s+/g, ''), 'base64')
  if (data.byteLength === 0) {
    return { ok: false, error: '参考图内容为空' }
  }
  if (data.byteLength > INIT_MAX_BYTES) {
    return { ok: false, error: `参考图过大(上限 ${INIT_MAX_BYTES / 1024 / 1024}MB)` }
  }
  return { ok: true, image: { data, ext: match[1] === 'jpg' ? 'jpeg' : match[1] } }
}
