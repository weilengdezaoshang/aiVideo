// BYOK 设置(S2/S2a/S2b)与首启引导(S1),线框《低保真线框图-GenCanvas.html》阶段 A。
// 服务端:/api/config GET(打码视图)/ POST(热切换);/api/config/test 测试连接。

const ONBOARDED_KEY = 'gencanvas.onboarded'

/** @param {string} sel @returns {HTMLElement} */
const $ = (sel) => /** @type {HTMLElement} */ (document.querySelector(sel))

/** Shoelace toast 提示 @param {string} message @param {string} [variant] */
async function toast(message, variant = 'success') {
  // sl-alert 可能尚未升级:先等定义完成,否则 .toast() 不存在且会中断调用方
  await customElements.whenDefined('sl-alert')
  const alert = /** @type {any} */ (
    Object.assign(document.createElement('sl-alert'), {
      variant,
      closable: true,
      duration: 3000,
      innerHTML: `<sl-icon name="${variant === 'success' ? 'check2-circle' : 'exclamation-triangle'}" slot="icon"></sl-icon>${message}`,
    })
  )
  document.body.append(alert)
  alert.toast()
}

/** @param {string} path @param {RequestInit} [options] */
async function api(path, options) {
  const res = await fetch(path, options)
  const body = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new Error(body.error || `请求失败 HTTP ${res.status}`)
  }
  return body
}

/**
 * @param {{
 *   onConfigChanged?: (config: { provider: string }) => void,
 * }} [hooks]
 */
/**
 * @param {{ onConfigChanged?: (config: { provider: string }) => void }} [hooks]
 */
export async function initSettings(hooks = {}) {
  // Shoelace 是运行时升级的自定义元素:未定义前 show/hide 不存在
  await customElements.whenDefined('sl-dialog')
  await customElements.whenDefined('sl-select')
  const dialog = /** @type {any} */ ($('#settings-dialog'))
  const onboarding = /** @type {any} */ ($('#onboarding-dialog'))
  const providerSelect = /** @type {any} */ ($('#settings-provider'))
  const comfyRow = $('#settings-comfy-row')
  const comfyUrl = /** @type {any} */ ($('#settings-comfy-url'))
  const cloudRow = $('#settings-cloud-row')
  const cloudUrlRow = $('#settings-cloud-url-row')
  const cloudVendor = /** @type {any} */ ($('#settings-cloud-vendor'))
  const cloudBaseUrl = /** @type {any} */ ($('#settings-cloud-base-url'))
  const cloudModel = /** @type {any} */ ($('#settings-cloud-model'))
  const testButton = /** @type {any} */ ($('#settings-test'))
  const testResult = $('#settings-test-result')
  const imageKey = /** @type {any} */ ($('#settings-image-key'))
  const videoKey = /** @type {any} */ ($('#settings-video-key'))

  /** 每次保存前重建:非空输入覆盖,clear 按钮发送空串(清除) */
  let clearImageKey = false
  let clearVideoKey = false

  /** 各后端的配置行只在其选中时有意义 */
  function syncRowVisibility() {
    comfyRow.hidden = providerSelect.value !== 'comfyui'
    const isCloud = providerSelect.value === 'cloud'
    cloudRow.hidden = !isCloud
    cloudUrlRow.hidden = !isCloud
  }

  /** @param {{ ok: boolean, detail: string }} [result] 测试结果条(S2a loading / S2b 失败 / 成功) */
  function showTestResult(result) {
    if (!result) {
      testResult.hidden = true
      return
    }
    testResult.hidden = false
    testResult.classList.toggle('error', !result.ok)
    testResult.textContent = result.ok ? `连接成功:${result.detail}` : `连接失败:${result.detail}`
  }

  async function refreshConfigView() {
    const { config } = await api('/api/config')
    providerSelect.value = config.provider
    comfyUrl.value = config.comfyUrl
    cloudVendor.value = config.cloudVendor || 'zhipu'
    cloudBaseUrl.value = config.cloudBaseUrl || ''
    cloudModel.value = config.cloudModel || ''
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
        }),
      })
      showTestResult(result)
    } catch (err) {
      showTestResult({ ok: false, detail: err instanceof Error ? err.message : String(err) })
    } finally {
      testButton.loading = false
      testButton.textContent = '测试连接'
    }
  })

  providerSelect.addEventListener('sl-change', syncRowVisibility)

  /* ---------- 保存:POST /api/config,热切换由服务端完成 ---------- */

  $('#settings-cancel').addEventListener('click', () => dialog.hide())
  $('#settings-save').addEventListener('click', async () => {
    /** @type {Record<string, unknown>} */
    const payload = { provider: providerSelect.value }
    const url = comfyUrl.value.trim()
    if (providerSelect.value === 'comfyui' || url) {
      payload.comfyUrl = url
    }
    if (providerSelect.value === 'cloud') {
      payload.cloudVendor = cloudVendor.value
      payload.cloudBaseUrl = cloudBaseUrl.value.trim()
      payload.cloudModel = cloudModel.value.trim()
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
    const saveButton = /** @type {any} */ ($('#settings-save'))
    saveButton.loading = true
    try {
      /** @type {any} */
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

  $('#settings-open').addEventListener('click', () => void openSettings())

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
    onboarding.show()
  }
}
