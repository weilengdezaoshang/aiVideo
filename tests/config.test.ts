import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import {
  applyConfigPatch,
  loadConfig,
  maskKey,
  publicConfig,
  saveConfigPatch,
} from '../src/config.js'
import type { AppConfig } from '../src/config.js'
import type { CloudVendorId } from '../src/services/providers/cloud-provider.js'
import { JobManager } from '../src/services/job-manager.js'
import { Store } from '../src/store.js'
import type { GenParams, Job } from '../src/types.js'
import type {
  GenContext,
  GeneratedImage,
  GenerationProvider,
} from '../src/services/providers/provider.js'

const baseConfig = (): AppConfig => ({
  provider: 'mock',
  comfyUrl: 'http://127.0.0.1:8188',
  port: 7801,
  imageTimeoutMin: 20,
  videoTimeoutMin: 60,
  videoModel: '',
  videoBackend: 'auto',
  wanClip: 'clip.safetensors',
  wanVae: 'vae.safetensors',
  wanHighNoiseUnet: '',
  wanLowNoiseUnet: '',
  imageApiKey: '',
  videoApiKey: '',
  cloudVendor: 'zhipu' as CloudVendorId,
  cloudBaseUrl: 'https://open.bigmodel.cn/api/paas/v4',
  cloudModel: 'cogview-4-250304',
  cloudTextModel: '',
})

/** 临时设置/还原环境变量,保证测试互不污染 */
function withEnv(env: Record<string, string | undefined>, fn: () => void): void {
  const saved = new Map<string, string | undefined>()
  for (const [key, value] of Object.entries(env)) {
    saved.set(key, process.env[key])
    if (value === undefined) {
      delete process.env[key]
    } else {
      process.env[key] = value
    }
  }
  try {
    fn()
  } finally {
    for (const [key, value] of saved) {
      if (value === undefined) {
        delete process.env[key]
      } else {
        process.env[key] = value
      }
    }
  }
}

/* ---------- loadConfig ---------- */

test('loadConfig 读 config.json,env 覆盖文件值', () => {
  withEnv({ SWARMUI_COMFY_URL: undefined, SWARMUI_PROVIDER: undefined }, () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'swarm-cfg-'))
    fs.writeFileSync(
      path.join(dir, 'config.json'),
      JSON.stringify({ provider: 'mock', comfyUrl: 'http://from-file:8188' }),
    )
    const fromFile = loadConfig(dir)
    assert.equal(fromFile.provider, 'mock')
    assert.equal(fromFile.comfyUrl, 'http://from-file:8188')

    process.env.SWARMUI_COMFY_URL = 'http://from-env:8188'
    const fromEnv = loadConfig(dir)
    assert.equal(fromEnv.comfyUrl, 'http://from-env:8188')
    delete process.env.SWARMUI_COMFY_URL
  })
})

test('loadConfig 非法 provider 快速失败', () => {
  withEnv({ SWARMUI_PROVIDER: undefined }, () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'swarm-cfg-'))
    fs.writeFileSync(path.join(dir, 'config.json'), JSON.stringify({ provider: 'nope' }))
    assert.throws(() => loadConfig(dir), /provider/)
  })
})

/* ---------- applyConfigPatch ---------- */

test('百炼配置:环境变量选择厂商和模型,空地址补丁使用默认 API', () => {
  withEnv(
    {
      SWARMUI_CLOUD_VENDOR: 'aliyun',
      SWARMUI_CLOUD_MODEL: 'qwen-image-2.0',
      SWARMUI_CLOUD_BASE_URL: 'https://ws-test.cn-beijing.maas.aliyuncs.com/api/v1',
    },
    () => {
      const config = loadConfig('/nonexistent-aivideo-config-test')
      assert.equal(config.cloudVendor, 'aliyun')
      assert.equal(config.cloudModel, 'qwen-image-2.0')
      assert.equal(config.cloudBaseUrl, 'https://ws-test.cn-beijing.maas.aliyuncs.com/api/v1')
      const result = applyConfigPatch(config, { cloudBaseUrl: '' }, {})
      assert.ok(result.ok)
      if (result.ok)
        {assert.equal(result.config.cloudBaseUrl, 'https://dashscope.aliyuncs.com/api/v1')}
    },
  )
})

test('applyConfigPatch:切换 provider 与 comfyUrl 合并生效', () => {
  withEnv({ SWARMUI_PROVIDER: undefined, SWARMUI_COMFY_URL: undefined }, () => {
    const result = applyConfigPatch(
      baseConfig(),
      { provider: 'comfyui', comfyUrl: 'http://192.168.1.5:8188 ' },
      {},
    )
    assert.ok(result.ok)
    if (!result.ok) {
      return
    }
    assert.deepEqual(result.filePatch, {
      provider: 'comfyui',
      comfyUrl: 'http://192.168.1.5:8188',
    })
    assert.equal(result.config.provider, 'comfyui')
    assert.equal(result.config.comfyUrl, 'http://192.168.1.5:8188')
    assert.deepEqual(result.envOverridden, [])
  })
})

test('applyConfigPatch:非法 provider / 非 http 地址被拒绝', () => {
  withEnv({ SWARMUI_PROVIDER: undefined, SWARMUI_COMFY_URL: undefined }, () => {
    const badProvider = applyConfigPatch(baseConfig(), { provider: 'openai' }, {})
    assert.equal(badProvider.ok, false)
    if (!badProvider.ok) {
      assert.match(badProvider.error, /provider/)
    }
    const badUrl = applyConfigPatch(baseConfig(), { comfyUrl: '192.168.1.5:8188' }, {})
    assert.equal(badUrl.ok, false)
    if (!badUrl.ok) {
      assert.match(badUrl.error, /comfyUrl/)
    }
  })
})

test('applyConfigPatch:Key 规范化、清除与超长拒绝;未变更不进 filePatch', () => {
  withEnv({ SWARMUI_IMAGE_API_KEY: undefined, SWARMUI_VIDEO_API_KEY: undefined }, () => {
    const current = { ...baseConfig(), imageApiKey: ' key-1234 ' }
    const set = applyConfigPatch(current, { imageApiKey: ' new-key ' }, {})
    assert.ok(set.ok)
    if (set.ok) {
      assert.deepEqual(set.filePatch, { imageApiKey: 'new-key' })
    }
    const clear = applyConfigPatch({ ...current, imageApiKey: 'new-key' }, { imageApiKey: '' }, {})
    assert.ok(clear.ok)
    if (clear.ok) {
      assert.deepEqual(clear.filePatch, { imageApiKey: '' })
    }
    const oversized = applyConfigPatch(current, { imageApiKey: 'k'.repeat(5000) }, {})
    assert.equal(oversized.ok, false)
    const noop = applyConfigPatch(
      { ...current, imageApiKey: 'new-key' },
      { imageApiKey: 'new-key' },
      {},
    )
    assert.ok(noop.ok)
    if (noop.ok) {
      assert.deepEqual(noop.filePatch, {})
    }
  })
})

test('applyConfigPatch:env 覆盖中的字段照写文件并在响应中提示', () => {
  withEnv({ SWARMUI_PROVIDER: 'mock', SWARMUI_COMFY_URL: undefined }, () => {
    const result = applyConfigPatch(
      baseConfig(),
      { provider: 'comfyui' },
      {
        SWARMUI_PROVIDER: 'mock',
      },
    )
    assert.ok(result.ok)
    if (result.ok) {
      assert.deepEqual(result.envOverridden, ['provider'])
      assert.equal(result.config.provider, 'comfyui')
    }
  })
})

/* ---------- saveConfigPatch ---------- */

test('saveConfigPatch 合并写回且保留未知字段;空补丁不写', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'swarm-cfg-save-'))
  fs.writeFileSync(
    path.join(dir, 'config.json'),
    JSON.stringify({ provider: 'mock', port: 7801, customField: 'keep-me' }),
  )
  await saveConfigPatch(dir, { provider: 'comfyui', comfyUrl: 'http://x:8188' })
  const merged = JSON.parse(fs.readFileSync(path.join(dir, 'config.json'), 'utf8'))
  assert.equal(merged.provider, 'comfyui')
  assert.equal(merged.comfyUrl, 'http://x:8188')
  assert.equal(merged.port, 7801)
  assert.equal(merged.customField, 'keep-me')

  const before = fs.readFileSync(path.join(dir, 'config.json'), 'utf8')
  await saveConfigPatch(dir, {})
  const after = fs.readFileSync(path.join(dir, 'config.json'), 'utf8')
  assert.equal(before, after)
})

/* ---------- 脱敏视图 ---------- */

test('maskKey 只留末 4 位;publicConfig 不泄露原文', () => {
  assert.equal(maskKey('sk-abcdef1234'), '••••1234')
  assert.equal(maskKey('abc'), '••••')
  assert.equal(maskKey(''), null)
  const view = publicConfig({ ...baseConfig(), imageApiKey: 'sk-secret-9999' })
  assert.equal(view.hasImageKey, true)
  assert.equal(view.imageApiKeyMasked, '••••9999')
  assert.equal(JSON.stringify(view).includes('sk-secret'), false)
})

/* ---------- JobManager.setProvider(热切换) ---------- */

const params: GenParams = {
  prompt: 'p',
  negativePrompt: '',
  model: 'm',
  width: 64,
  height: 64,
  steps: 2,
  cfgScale: 7,
  seed: 100,
  batchCount: 1,
  sampler: 'euler',
  scheduler: 'normal',
  denoise: 1,
  kind: 'image',
  durationSec: 4,
  fps: 16,
}

function fakeProvider(overrides: Partial<GenerationProvider> = {}): GenerationProvider {
  return {
    name: 'fake',
    capacity: 1,
    async status() {
      return { ok: true, detail: 'ok' }
    },
    async listModels() {
      return []
    },
    async listSamplerOptions() {
      return { samplers: ['euler'], schedulers: ['normal'] }
    },
    async generate(_params: GenParams, ctx: GenContext): Promise<GeneratedImage> {
      ctx.onProgress(1, 'done')
      return { data: Buffer.from('img'), ext: 'png' }
    },
    async generateVideo(_params: GenParams, ctx: GenContext): Promise<GeneratedImage> {
      ctx.onProgress(1, 'done')
      return { data: Buffer.from('vid'), ext: 'svg' }
    },
    ...overrides,
  }
}

function waitEvent(
  jm: JobManager,
  event: string,
  predicate: (payload: Job) => boolean,
): Promise<Job> {
  return new Promise((resolve) => {
    const handler = (payload: Job) => {
      if (predicate(payload)) {
        jm.off(event, handler)
        resolve(payload)
      }
    }
    jm.on(event, handler)
  })
}

test('setProvider 热切换:切换前任务走旧后端,切换后走新后端', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'swarm-jm-switch-'))
  const store = new Store(path.join(dir, 'images'), path.join(dir, 'history.json'))
  await store.init()
  const jm = new JobManager(fakeProvider({ name: 'old' }), store)

  const done1 = waitEvent(jm, 'job', (j) => j.status === 'completed')
  jm.createJob({ ...params, seed: 1 })
  await done1

  jm.setProvider(fakeProvider({ name: 'new' }))
  const done2 = waitEvent(jm, 'job', (j) => j.status === 'completed')
  jm.createJob({ ...params, seed: 2 })
  await done2

  const records = store.list(10)
  const bySeed = new Map(records.map((r) => [r.params.seed, r.provider]))
  assert.equal(bySeed.get(1), 'old')
  assert.equal(bySeed.get(2), 'new')
})
