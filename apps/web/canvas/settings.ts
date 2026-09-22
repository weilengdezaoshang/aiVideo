// BYOK 设置(S2/S2a/S2b)与首启引导(S1),线框《低保真线框图-GenCanvas.html》阶段 A。
// 服务端:/api/config GET(打码视图)/ POST(热切换);/api/config/test 测试连接。

const ONBOARDED_KEY = 'gencanvas.onboarded'

const $ = (sel: string) => document.querySelector<HTMLElement>(sel)!

/** Shoelace 自定义元素无类型来源,统一走宽松接口 */
/* eslint-disable @typescript-eslint/no-explicit-any -- Shoelace 边界 */

/** Shoelace toast 提示 */
async function toast(message: string, variant = 'success') {
  // sl-alert 可能尚未升级:先等定义完成,否则 .toast() 不存在且会中断调用方
  await customElements.whenDefined('sl-alert')
  const alert = Object.assign(document.createElement('sl-alert'), {
    variant,
    closable: true,
    duration: 3000,
    innerHTML: `<sl-icon name="${variant === 'success' ? 'check2-circle' : 'exclamation-triangle'}" slot="icon"></sl-icon>${message}`,
  }) as any
  document.body.append(alert)
  alert.toast()
}

async function api(path: string, options?: RequestInit): Promise<any> {
  const res = await fetch(path, { ...options, signal: AbortSignal.timeout(15000) })
  const body = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new Error(body.error || `请求失败 HTTP ${res.status}`)
  }
  return body
}

export async function initSettings(
  hooks: { onConfigChanged?: (config: { provider: string }) => void } = {},
) {
  const runtime = await api('/api/runtime').catch(() => null)
  if (runtime?.settingsEnabled !== true) {
    return
  }
  $('#user-menu-settings').hidden = false
  // Shoelace 是运行时升级的自定义元素:未定义前 show/hide 不存在
  await customElements.whenDefined('sl-dialog')
  const dialog = $('#settings-dialog') as any
  const onboarding = $('#onboarding-dialog') as any
  const providerSelect = $('#settings-provider') as any
  const comfyRow = $('#settings-comfy-row')
  const comfyUrl = $('#settings-comfy-url') as any
  const cloudRow = $('#settings-cloud-row')
  const cloudUrlRow = $('#settings-cloud-url-row')
  const cloudTextRow = $('#settings-cloud-text-row')
  const cloudVendor = $('#settings-cloud-vendor') as any
  const cloudBaseUrl = $('#settings-cloud-base-url') as any
  const cloudModel = $('#settings-cloud-model') as any
  const videoModel = $('#settings-video-model') as HTMLInputElement
  const videoModelRow = $('#settings-video-model-row')
  const cloudTextModel = $('#settings-cloud-text-model') as any
  const testButton = $('#settings-test') as any
  const testResult = $('#settings-test-result')
  const imageKey = $('#settings-image-key') as any
  const videoKey = $('#settings-video-key') as any

  /** 每次保存前重建:非空输入覆盖,clear 按钮发送空串(清除) */
  let clearImageKey = false
  let clearVideoKey = false

  /** 各后端的配置行只在其选中时有意义 */
  function syncRowVisibility() {
    comfyRow.hidden = providerSelect.value !== 'comfyui'
    const isCloud = providerSelect.value === 'cloud'
    cloudRow.hidden = !isCloud
    cloudUrlRow.hidden = !isCloud
    videoModelRow.hidden = !isCloud || cloudVendor.value !== 'aliyun'
    cloudTextRow.hidden = !isCloud || cloudVendor.value === 'aliyun'
  }

  /** 测试结果条(S2a loading / S2b 失败 / 成功) */
  function showTestResult(result?: { ok: boolean; detail: string }) {
    if (!result) {
      testResult.hidden = true
      return
    }
    testResult.hidden = false
    testResult.classList.toggle('error', !result.ok)
    testResult.textContent = result.ok ? `检查通过:${result.detail}` : `检查失败:${result.detail}`
  }

  async function refreshConfigView() {
    const { config } = await api('/api/config')
    providerSelect.value = config.provider
    comfyUrl.value = config.comfyUrl
    cloudVendor.value = config.cloudVendor || 'zhipu'
    cloudBaseUrl.value = config.cloudBaseUrl || ''
    cloudModel.value = config.cloudModel || ''
    videoModel.value = config.videoModel || ''
    cloudTextModel.value = config.cloudTextModel || ''
    syncRowVisibility()
    showTestResult()
    imageKey.value = ''
    videoKey.value = ''
    clearImageKey = false
    clearVideoKey = false
    imageKey.placeholder = config.hasImageKey
      ? `图像 API Key(已保存 ${config.imageApiKeyMasked ?? ''})`
      : '图像 API Key…'
    videoKey.placeholder = config.hasVideoKey
      ? `视频 API Key(已保存 ${config.videoApiKeyMasked ?? ''})`
      : '视频 API Key…'
  }

  async function openSettings() {
    try {
      await refreshConfigView()
    } catch (err) {
      toast(`读取配置失败:${err instanceof Error ? err.message : String(err)}`, 'danger')
    }
    dialog.show()
  }

  /* ---------- S2a/S2b:测试连接(loading → 结果条,失败留在本页可重试) ---------- */

  testButton.addEventListener('click', async () => {
    testButton.loading = true
    testButton.textContent = '测试中…'
    showTestResult()
    try {
      const result = await api('/api/config/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          provider: providerSelect.value,
          comfyUrl: comfyUrl.value.trim(),
          cloudVendor: cloudVendor.value,
          cloudBaseUrl: cloudBaseUrl.value.trim(),
          cloudModel: cloudModel.value.trim(),
          cloudTextModel: cloudTextModel.value.trim(),
          ...(clearImageKey
            ? { imageApiKey: '' }
            : imageKey.value.trim()
              ? { imageApiKey: imageKey.value.trim() }
              : {}),
        }),
      })
      showTestResult(result)
    } catch (err) {
      showTestResult({ ok: false, detail: err instanceof Error ? err.message : String(err) })
    } finally {
      testButton.loading = false
      testButton.textContent = '检查配置 / 连接'
    }
  })

  providerSelect.addEventListener('change', syncRowVisibility)
  cloudVendor.addEventListener('change', () => {
    const defaults: Record<string, string[]> = {
      aliyun: ['https://dashscope.aliyuncs.com/api/v1', 'qwen-image-2.0'],
      zhipu: ['https://open.bigmodel.cn/api/paas/v4', 'cogview-4-250304'],
      siliconflow: ['https://api.siliconflow.cn/v1', 'black-forest-labs/FLUX.1-schnell'],
      openai: ['https://api.openai.com/v1', 'gpt-image-1'],
    }
    const selected = defaults[cloudVendor.value]
    if (selected) {
      cloudBaseUrl.value = selected[0]
      cloudModel.value = selected[1]
      cloudTextModel.value = ''
    }
    syncRowVisibility()
  })

  /* ---------- 保存:POST /api/config,热切换由服务端完成 ---------- */

  $('#settings-cancel').addEventListener('click', () => dialog.hide())
  $('#settings-save').addEventListener('click', async () => {
    const payload: Record<string, unknown> = { provider: providerSelect.value }
    const url = comfyUrl.value.trim()
    if (providerSelect.value === 'comfyui' || url) {
      payload.comfyUrl = url
    }
    if (providerSelect.value === 'cloud') {
      payload.cloudVendor = cloudVendor.value
      payload.cloudBaseUrl = cloudBaseUrl.value.trim()
      payload.cloudModel = cloudModel.value.trim()
      payload.videoModel = videoModel.value.trim()
      payload.cloudTextModel = cloudTextModel.value.trim()
      if (!String(cloudModel.value || '').trim()) {
        toast('请填写云端模型名(如 cogview-4-250304)', 'warning')
        cloudModel.focus()
        return
      }
    }
    if (clearImageKey) {
      payload.imageApiKey = ''
    } else if (imageKey.value.trim()) {
      payload.imageApiKey = imageKey.value.trim()
    }
    if (clearVideoKey) {
      payload.videoApiKey = ''
    } else if (videoKey.value.trim()) {
      payload.videoApiKey = videoKey.value.trim()
    }
    const saveButton = $('#settings-save') as any
    saveButton.loading = true
    try {
      const result = await api('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      localStorage.setItem(ONBOARDED_KEY, '1')
      dialog.hide()
      if (result.providerSwitched) {
        const label =
          result.config.provider === 'comfyui'
            ? 'ComfyUI'
            : result.config.provider === 'cloud'
              ? `云端模型 ${result.config.cloudModel}`
              : '演示模式'
        toast(`已切换生成后端:${label}`)
      } else {
        toast('设置已保存')
      }
      if (result.envOverridden?.length > 0) {
        toast(`注意:${result.envOverridden.join('、')} 由环境变量覆盖,本次修改未生效`, 'warning')
      }
      hooks.onConfigChanged?.(result.config)
    } catch (err) {
      toast(`保存失败:${err instanceof Error ? err.message : String(err)}`, 'danger')
    } finally {
      saveButton.loading = false
    }
  })

  /* ---------- Key 清除按钮:显式发送空串 ---------- */

  $('#settings-image-key-clear').addEventListener('click', () => {
    clearImageKey = true
    imageKey.value = ''
    imageKey.placeholder = '图像 API Key(保存时清除)'
  })
  $('#settings-video-key-clear').addEventListener('click', () => {
    clearVideoKey = true
    videoKey.value = ''
    videoKey.placeholder = '视频 API Key(保存时清除)'
  })

  /* ---------- S1 首启引导 ---------- */

  $('#user-menu-settings').addEventListener('click', () => void openSettings())

  function markOnboarded() {
    localStorage.setItem(ONBOARDED_KEY, '1')
  }

  $('#onb-comfy').addEventListener('click', () => {
    markOnboarded()
    onboarding.hide()
    void openSettings().then(() => {
      providerSelect.value = 'comfyui'
      syncRowVisibility()
      comfyUrl.focus()
    })
  })
  $('#onb-byok').addEventListener('click', () => {
    markOnboarded()
    onboarding.hide()
    void openSettings()
  })
  $('#onb-demo').addEventListener('click', () => {
    markOnboarded()
    onboarding.hide()
    toast('演示模式:Mock 后端已就绪,按 Tab 开始第一张图')
  })
  // 引导框被 Esc/关闭按钮关掉也视为已见,避免每次刷新都打扰
  onboarding.addEventListener('sl-after-hide', markOnboarded)

  if (!localStorage.getItem(ONBOARDED_KEY)) {
    try {
      const { config } = await api('/api/config')
      if (config.provider === 'mock') {
        onboarding.show()
      } else {
        markOnboarded()
      }
    } catch {
      onboarding.show()
    }
  }
}
