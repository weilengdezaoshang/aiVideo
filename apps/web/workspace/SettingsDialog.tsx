import { useEffect, useState } from 'react'
import { Button, Dialog, Select } from '../ui/primitives.js'
import { json, request } from './api.js'

type PublicConfig = { provider: string; comfyUrl: string }
export function SettingsDialog({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [config, setConfig] = useState<PublicConfig | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState('')
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    void request<{ config: PublicConfig }>('/api/config', { signal: controller.signal })
      .then((body) => {
        if (!controller.signal.aborted) {
          setConfig(body.config)
        }
      })
      .catch((err) => {
        if (!controller.signal.aborted) {
          setError(String(err))
        }
      })
    return () => controller.abort()
  }, [revision])
  async function save(test: boolean) {
    if (!config) {
      return
    }
    setBusy(true)
    setError('')
    setResult('')
    try {
      const body = await request<{ ok?: boolean; detail?: string }>(
        test ? '/api/config/test' : '/api/config',
        json({ provider: config.provider, comfyUrl: config.comfyUrl }),
      )
      if (test) {
        setResult(`${body.ok ? '连接成功' : '连接失败'}：${body.detail || ''}`)
      } else {
        onSaved()
        onClose()
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '设置保存失败')
    } finally {
      setBusy(false)
    }
  }
  return (
    <Dialog title="生成设置" onClose={busy ? () => {} : onClose}>
      {config ? (
        <>
          <p className="muted">选择生成后端。模型列表会随配置自动更新。</p>
          <label className="settings-row">
            生成后端
            <Select
              label="生成后端"
              value={config.provider}
              onChange={(provider) => {
                setConfig({ ...config, provider })
                setResult('')
              }}
              disabled={busy}
              options={[
                { value: 'mock', label: 'Mock · 演示' },
                { value: 'comfyui', label: 'ComfyUI' },
                { value: 'cloud', label: '云端 · 现有配置' },
              ]}
            />
          </label>
          {config.provider === 'comfyui' && (
            <label className="settings-row">
              ComfyUI 地址
              <input
                aria-label="ComfyUI 地址"
                value={config.comfyUrl}
                disabled={busy}
                onChange={(event) => setConfig({ ...config, comfyUrl: event.target.value })}
              />
            </label>
          )}
          {config.provider === 'cloud' && (
            <p className="muted">
              使用已配置的云端服务。API Key 与厂商参数可在画布右上角的设置中管理。
            </p>
          )}
          <div className="dialog-actions">
            <Button disabled={busy} onClick={() => void save(true)}>
              测试连接
            </Button>
            <Button variant="primary" disabled={busy} onClick={() => void save(false)}>
              {busy ? '处理中…' : '保存设置'}
            </Button>
          </div>
        </>
      ) : (
        <p>正在读取设置…</p>
      )}
      {error && (
        <div role="alert" className="inline-error">
          {error}
          {!config && (
            <Button
              onClick={() => {
                setError('')
                setRevision((value) => value + 1)
              }}
            >
              重试
            </Button>
          )}
        </div>
      )}
      {result && <p role="status">{result}</p>}
    </Dialog>
  )
}
