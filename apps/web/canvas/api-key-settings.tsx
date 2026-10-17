import { useEffect, useRef, useState } from 'react'

interface ConfigStatus {
  hostEnabled: boolean
  configured: boolean
  keyConfigured: boolean
  baseUrl: string
  model: string
  timeoutMs: number
}

// TODO(admin): 本机联调的临时入口，后续改由后台管理模型与凭据。
// 密钥只在提交时发送到同源服务端；不写 localStorage、画布文档或控制台。
export function ApiKeySettings({ onClose }: { onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const keyInput = useRef<HTMLInputElement>(null)
  const [status, setStatus] = useState<ConfigStatus | null>(null)
  const [baseUrl, setBaseUrl] = useState('https://dashscope.aliyuncs.com/compatible-mode/v1')
  const [model, setModel] = useState('qwen-plus')
  const [timeoutMs, setTimeoutMs] = useState(45000)
  const [busy, setBusy] = useState(false)
  const [ready, setReady] = useState(false)
  const [message, setMessage] = useState('')
  useEffect(() => {
    dialog.current?.showModal()
    const controller = new AbortController()
    void fetch('/api/creation/v1/llm-config', { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) {
          throw new Error('配置服务未启用，请检查服务端配置及管理员权限。')
        }
        const data = (await response.json()) as ConfigStatus
        if (controller.signal.aborted) {
          return
        }
        setStatus(data)
        if (data.configured) {
          setBaseUrl(data.baseUrl)
          setModel(data.model)
          setTimeoutMs(data.timeoutMs)
        }
        setReady(true)
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setMessage('配置服务未启用或暂时不可用，请检查服务端配置及管理员权限。')
        }
      })
    return () => {
      controller.abort()
      if (keyInput.current) {
        keyInput.current.value = ''
      }
    }
  }, [])
  return (
    <dialog
      ref={dialog}
      className="canvas-api-settings"
      aria-labelledby="api-settings-title"
      onCancel={onClose}
    >
      <header className="api-settings-header">
        <span className="api-settings-emblem" aria-hidden="true">
          <svg
            width="22"
            height="22"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <circle cx="8" cy="8" r="4.5" />
            <path d="m11.2 11.2 9.3 9.3m-4-4 2.5-2.5m-5 0 2.5-2.5" />
          </svg>
        </span>
        <div>
          <h2 id="api-settings-title">配置 API Key</h2>
          <p className="api-settings-subtitle">连接你的对话模型</p>
        </div>
        <button
          className="api-settings-close"
          type="button"
          aria-label="关闭 API Key 配置"
          onClick={onClose}
        >
          <svg
            width="18"
            height="18"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.7"
            strokeLinecap="round"
            aria-hidden="true"
          >
            <path d="m6 6 12 12M18 6 6 18" />
          </svg>
        </button>
      </header>
      <form
        onSubmit={(event) => {
          event.preventDefault()
          if (busy || !ready) {
            return
          }
          const apiKey = keyInput.current?.value.trim() ?? ''
          setBusy(true)
          setMessage('')
          void fetch('/api/creation/v1/llm-config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ baseUrl, model, timeoutMs, ...(apiKey ? { apiKey } : {}) }),
          })
            .then(async (response) => {
              if (!response.ok) {
                throw new Error('保存失败，请检查配置及服务端文件权限。')
              }
              setStatus((await response.json()) as ConfigStatus)
              setMessage(
                status?.hostEnabled
                  ? '已保存。当前创作助手仍使用模板规划。'
                  : '已保存。对话模型新运行使用新配置。',
              )
            })
            .catch(() => setMessage('保存失败，请检查配置、管理员权限及服务端文件权限。'))
            .finally(() => {
              if (keyInput.current) {
                keyInput.current.value = ''
              }
              setBusy(false)
            })
        }}
      >
        <div className="api-settings-fields">
          <label>
            模型名称
            <input
              required
              value={model}
              maxLength={200}
              onChange={(event) => setModel(event.target.value)}
              disabled={!ready || busy}
              placeholder="例如 qwen-plus"
            />
          </label>
          <label>
            接口地址
            <input
              className="api-settings-url"
              type="url"
              required
              value={baseUrl}
              onChange={(event) => setBaseUrl(event.target.value)}
              disabled={!ready || busy}
            />
          </label>
          <label>
            <span className="api-settings-label-row">
              <span>API Key</span>
              {ready && (
                <span className="api-settings-key-state">
                  {status?.keyConfigured ? '已配置' : '未配置'}
                </span>
              )}
            </span>
            <input
              ref={keyInput}
              aria-label="API Key"
              type="password"
              autoComplete="new-password"
              maxLength={4096}
              required={!status?.keyConfigured}
              placeholder={status?.keyConfigured ? '已配置，留空保留原 Key' : '输入模型 API Key'}
              disabled={!ready || busy}
            />
          </label>
          <p className="api-settings-key-hint">
            {status?.keyConfigured
              ? '留空保留原 Key，输入新 Key 可替换。'
              : '使用对话模型的 API Key，与生图凭据分开配置。'}
          </p>
          {status?.hostEnabled && (
            <p className="api-settings-key-hint">
              当前创作助手采用模板规划，此配置不会改变生图、视频路由。
            </p>
          )}
          <details className="api-settings-advanced">
            <summary>
              高级设置
              <svg
                width="16"
                height="16"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.7"
                aria-hidden="true"
              >
                <path d="m9 5 7 7-7 7" />
              </svg>
            </summary>
            <div className="api-settings-advanced-body">
              <label>
                超时（毫秒）
                <input
                  type="number"
                  min={30000}
                  max={60000}
                  step={1000}
                  required
                  value={timeoutMs}
                  onChange={(event) => setTimeoutMs(Number(event.target.value))}
                  disabled={!ready || busy}
                />
              </label>
            </div>
          </details>
          {message && (
            <p
              className={
                message.startsWith('已保存')
                  ? 'api-settings-feedback is-success'
                  : 'api-settings-feedback is-error'
              }
              role="status"
            >
              {message}
            </p>
          )}
        </div>
        <footer>
          <p>对话模型配置按工作区保存</p>
          <div className="api-settings-actions">
            <button className="api-settings-secondary" type="button" onClick={onClose}>
              取消
            </button>
            <button className="api-settings-primary" type="submit" disabled={!ready || busy}>
              {busy ? '保存中…' : '保存配置'}
            </button>
          </div>
        </footer>
      </form>
    </dialog>
  )
}
