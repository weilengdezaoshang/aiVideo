// 运行配置:读取、校验、热更新(config.json)。
// 环境变量(SWARMUI_*)优先级高于文件;运行中的修改(POST /api/config)只写文件,
// 不触碰 env —— 与"启动时校验快速失败"的既有语义保持一致(PRD F8 BYOK)。

import { readFileSync } from 'node:fs'
import { promises as fs } from 'node:fs'
import path from 'node:path'
import { CLOUD_VENDOR_IDS, CLOUD_VENDORS } from './services/providers/cloud-provider.js'
import type { CloudVendorId } from './services/providers/cloud-provider.js'

export interface AppConfig {
  provider: 'mock' | 'comfyui' | 'cloud'
  comfyUrl: string
  /** 云端生图厂商(zhipu/siliconflow/openai) */
  cloudVendor: CloudVendorId
  /** 云端 API 地址(厂商默认可覆盖,兼容自建网关) */
  cloudBaseUrl: string
  /** 云端模型名(如 cogview-4-250304) */
  cloudModel: string
  /** 云端文本模型(中译英用;留空 = 关闭翻译直通) */
  cloudTextModel: string
  port: number
  /** ComfyUI 图像任务整体超时(分钟) */
  imageTimeoutMin: number
  /** ComfyUI 视频任务整体超时(分钟) */
  videoTimeoutMin: number
  /** 视频模型文件名;留空自动探测 */
  videoModel: string
  videoBackend: 'auto' | 'ltxv' | 'wan'
  wanClip: string
  wanVae: string
  wanHighNoiseUnet: string
  wanLowNoiseUnet: string
  /** BYOK 云端图像 API Key(首批接入清单待定,PRD §11-4;先存管,消费方后续接入) */
  imageApiKey: string
  /** BYOK 云端视频 API Key */
  videoApiKey: string
}

const readOptionalString = (envKey: string, fileValue: unknown): string => {
  const env = process.env[envKey]
  if (env !== undefined && env.trim() !== '') {
    return env.trim()
  }
  return typeof fileValue === 'string' ? fileValue.trim() : ''
}

const readTimeoutMin = (envKey: string, fileValue: unknown, fallback: number): number => {
  const raw = process.env[envKey] ?? fileValue
  if (raw === undefined || raw === '') {
    return fallback
  }
  const n = Number(raw)
  if (!Number.isFinite(n) || n < 1 || n > 240) {
    throw new Error(`配置错误:${envKey} 必须是 1..240 的分钟数,当前为 "${raw}"`)
  }
  return n
}

/** 读取并校验运行配置:错误直接抛出快速失败,避免带病启动。 */
export function loadConfig(rootDir: string): AppConfig {
  let file: Partial<AppConfig> = {}
  try {
    file = JSON.parse(readFileSync(path.join(rootDir, 'config.json'), 'utf8'))
  } catch {
    // 没有 config.json 时使用默认值
  }
  const provider = process.env.SWARMUI_PROVIDER ?? file.provider ?? 'mock'
  if (provider !== 'mock' && provider !== 'comfyui' && provider !== 'cloud') {
    throw new Error(`配置错误:provider 只能是 mock、comfyui 或 cloud,当前为 "${provider}"`)
  }
  const cloudVendorRaw = readOptionalString('SWARMUI_CLOUD_VENDOR', file.cloudVendor) || 'zhipu'
  if (!CLOUD_VENDOR_IDS.includes(cloudVendorRaw as never)) {
    throw new Error(
      `配置错误:cloudVendor 只能是 ${CLOUD_VENDOR_IDS.join(' / ')},当前为 "${cloudVendorRaw}"`,
    )
  }
  const cloudVendor = cloudVendorRaw as CloudVendorId
  const cloudBaseUrl = readOptionalString('SWARMUI_CLOUD_BASE_URL', file.cloudBaseUrl)
  const cloudModel = readOptionalString('SWARMUI_CLOUD_MODEL', file.cloudModel)
  const cloudTextModel = readOptionalString('SWARMUI_CLOUD_TEXT_MODEL', file.cloudTextModel)
  const comfyUrl = process.env.SWARMUI_COMFY_URL ?? file.comfyUrl ?? 'http://127.0.0.1:8188'
  if (provider === 'comfyui' && !/^https?:\/\//.test(comfyUrl)) {
    throw new Error(`配置错误:comfyUrl 必须以 http:// 或 https:// 开头,当前为 "${comfyUrl}"`)
  }
  const port = Number(process.env.SWARMUI_PORT ?? file.port ?? 7801)
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(`配置错误:port 必须是 1..65535 的整数,当前为 "${port}"`)
  }
  const videoBackendRaw = readOptionalString('SWARMUI_VIDEO_BACKEND', file.videoBackend) || 'auto'
  if (videoBackendRaw !== 'auto' && videoBackendRaw !== 'ltxv' && videoBackendRaw !== 'wan') {
    throw new Error(`配置错误:videoBackend 只能是 auto / ltxv / wan,当前为 "${videoBackendRaw}"`)
  }
  return {
    provider,
    comfyUrl,
    port,
    imageTimeoutMin: readTimeoutMin('SWARMUI_IMAGE_TIMEOUT_MIN', file.imageTimeoutMin, 20),
    videoTimeoutMin: readTimeoutMin('SWARMUI_VIDEO_TIMEOUT_MIN', file.videoTimeoutMin, 60),
    videoModel: readOptionalString('SWARMUI_VIDEO_MODEL', file.videoModel),
    videoBackend: videoBackendRaw,
    wanClip:
      readOptionalString('SWARMUI_WAN_CLIP', file.wanClip) ||
      'umt5_xxl_fp8_e4m3fn_scaled.safetensors',
    wanVae: readOptionalString('SWARMUI_WAN_VAE', file.wanVae) || 'wan_2.1_vae.safetensors',
    wanHighNoiseUnet: readOptionalString('SWARMUI_WAN_HIGH_NOISE_UNET', file.wanHighNoiseUnet),
    wanLowNoiseUnet: readOptionalString('SWARMUI_WAN_LOW_NOISE_UNET', file.wanLowNoiseUnet),
    imageApiKey: readOptionalString('SWARMUI_IMAGE_API_KEY', file.imageApiKey),
    videoApiKey: readOptionalString('SWARMUI_VIDEO_API_KEY', file.videoApiKey),
    cloudVendor,
    cloudBaseUrl: cloudBaseUrl || CLOUD_VENDORS[cloudVendor].defaultBaseUrl,
    cloudModel: cloudModel || CLOUD_VENDORS[cloudVendor].defaultModel,
    cloudTextModel,
  }
}

/** 运行中允许修改的字段(BYOK / 后端切换);port 与超时等需重启,不接受热改。 */
export type ConfigPatch = {
  provider?: unknown
  comfyUrl?: unknown
  imageApiKey?: unknown
  videoApiKey?: unknown
  cloudVendor?: unknown
  cloudBaseUrl?: unknown
  cloudModel?: unknown
  cloudTextModel?: unknown
}

/** 本次实际变更、允许写回 config.json 的字段集合 */
export type ConfigFilePatch = Partial<
  Pick<
    AppConfig,
    | 'provider'
    | 'comfyUrl'
    | 'imageApiKey'
    | 'videoApiKey'
    | 'cloudVendor'
    | 'cloudBaseUrl'
    | 'cloudModel'
    | 'cloudTextModel'
  >
>

export type PatchResult =
  | {
      ok: true
      config: AppConfig
      filePatch: ConfigFilePatch
      /** 声明了 env 覆盖的字段:文件已写入,但生效值仍来自 env */
      envOverridden: string[]
    }
  | { ok: false; error: string }

/** 校验 API Key 补丁值:合法返回规范化字符串(空串 = 清除),非法返回错误文案。 */
function normalizeKey(
  value: unknown,
  label: string,
): { ok: true; value: string } | { ok: false; error: string } {
  if (typeof value !== 'string') {
    return { ok: false, error: `配置错误:${label} 必须是字符串` }
  }
  const trimmed = value.trim()
  if (trimmed.length > 4096) {
    return { ok: false, error: `配置错误:${label} 过长(上限 4096 字符)` }
  }
  return { ok: true, value: trimmed }
}

/** applyConfigPatch 的 env 视图:只关心可能覆盖补丁字段的两个变量 */
export type ConfigEnv = {
  SWARMUI_PROVIDER?: string | undefined
  SWARMUI_COMFY_URL?: string | undefined
}

/**
 * 校验并应用配置补丁(纯函数,可 node:test)。
 * 补丁字段缺省 = 不变;Key 传空字符串 = 清除。
 */
export function applyConfigPatch(
  current: AppConfig,
  patch: ConfigPatch,
  env: ConfigEnv,
): PatchResult {
  const filePatch: ConfigFilePatch = {}
  const envOverridden: string[] = []
  if (patch.cloudVendor !== undefined) {
    if (
      typeof patch.cloudVendor !== 'string' ||
      !CLOUD_VENDOR_IDS.includes(patch.cloudVendor as never)
    ) {
      return { ok: false, error: `配置错误:cloudVendor 只能是 ${CLOUD_VENDOR_IDS.join(' / ')}` }
    }
    if (patch.cloudVendor !== current.cloudVendor) {
      filePatch.cloudVendor = patch.cloudVendor as CloudVendorId
    }
  }
  if (patch.cloudBaseUrl !== undefined) {
    if (typeof patch.cloudBaseUrl !== 'string') {
      return { ok: false, error: '配置错误:cloudBaseUrl 必须是字符串' }
    }
    const cloudUrl = patch.cloudBaseUrl.trim()
    if (cloudUrl && !/^https?:\/\//.test(cloudUrl)) {
      return { ok: false, error: '配置错误:cloudBaseUrl 必须以 http:// 或 https:// 开头' }
    }
    if (cloudUrl !== current.cloudBaseUrl) {
      filePatch.cloudBaseUrl = cloudUrl
    }
  }
  if (patch.cloudModel !== undefined) {
    if (typeof patch.cloudModel !== 'string') {
      return { ok: false, error: '配置错误:cloudModel 必须是字符串' }
    }
    const cloudModel = patch.cloudModel.trim().slice(0, 200)
    if (cloudModel && cloudModel !== current.cloudModel) {
      filePatch.cloudModel = cloudModel
    }
  }
  if (patch.cloudTextModel !== undefined) {
    if (typeof patch.cloudTextModel !== 'string') {
      return { ok: false, error: '配置错误:cloudTextModel 必须是字符串' }
    }
    const cloudTextModel = patch.cloudTextModel.trim().slice(0, 200)
    if (cloudTextModel !== current.cloudTextModel) {
      filePatch.cloudTextModel = cloudTextModel
    }
  }
  const keyEnvNames = { imageApiKey: 'SWARMUI_IMAGE_API_KEY', videoApiKey: 'SWARMUI_VIDEO_API_KEY' }

  if (patch.provider !== undefined) {
    if (patch.provider !== 'mock' && patch.provider !== 'comfyui' && patch.provider !== 'cloud') {
      return { ok: false, error: `配置错误:provider 只能是 mock、comfyui 或 cloud` }
    }
    if (patch.provider !== current.provider) {
      filePatch.provider = patch.provider
      if (env.SWARMUI_PROVIDER) {
        envOverridden.push('provider')
      }
    }
  }
  if (patch.comfyUrl !== undefined) {
    if (typeof patch.comfyUrl !== 'string') {
      return { ok: false, error: '配置错误:comfyUrl 必须是字符串' }
    }
    const url = patch.comfyUrl.trim()
    if (!/^https?:\/\//.test(url)) {
      return { ok: false, error: '配置错误:comfyUrl 必须以 http:// 或 https:// 开头' }
    }
    if (url !== current.comfyUrl) {
      filePatch.comfyUrl = url
      if (env.SWARMUI_COMFY_URL) {
        envOverridden.push('comfyUrl')
      }
    }
  }
  for (const [field, label] of [
    ['imageApiKey', '图像 API Key'],
    ['videoApiKey', '视频 API Key'],
  ] as const) {
    const raw = patch[field]
    if (raw === undefined) {
      continue
    }
    const normalized = normalizeKey(raw, label)
    if (!normalized.ok) {
      return { ok: false, error: normalized.error }
    }
    if (normalized.value !== current[field]) {
      filePatch[field] = normalized.value
      if (normalized.value !== '' && process.env[keyEnvNames[field]]) {
        envOverridden.push(field)
      }
    }
  }

  return {
    ok: true,
    config: { ...current, ...filePatch },
    filePatch,
    envOverridden,
  }
}

/** 把补丁合并写回 config.json(原子写;保留本程序不认识的字段)。 */
export async function saveConfigPatch(rootDir: string, filePatch: ConfigFilePatch): Promise<void> {
  if (Object.keys(filePatch).length === 0) {
    return
  }
  const file = path.join(rootDir, 'config.json')
  let existing: Record<string, unknown> = {}
  try {
    existing = JSON.parse(await fs.readFile(file, 'utf8'))
  } catch {
    // 首次写入:没有文件或文件损坏都从空对象开始
  }
  const merged = { ...existing, ...filePatch }
  const tmp = `${file}.tmp`
  await fs.writeFile(tmp, JSON.stringify(merged, null, 2) + '\n')
  await fs.rename(tmp, file)
}

/** Key 脱敏展示:只保留末 4 位。 */
export function maskKey(key: string): string | null {
  if (!key) {
    return null
  }
  return key.length <= 4 ? '••••' : `••••${key.slice(-4)}`
}

/** 对外(前端)配置视图:Key 只出掩码,不出原文。 */
export function publicConfig(config: AppConfig) {
  return {
    provider: config.provider,
    comfyUrl: config.comfyUrl,
    cloudVendor: config.cloudVendor,
    cloudBaseUrl: config.cloudBaseUrl,
    cloudModel: config.cloudModel,
    cloudTextModel: config.cloudTextModel,
    imageApiKeyMasked: maskKey(config.imageApiKey),
    videoApiKeyMasked: maskKey(config.videoApiKey),
    hasImageKey: Boolean(config.imageApiKey),
    hasVideoKey: Boolean(config.videoApiKey),
  }
}
