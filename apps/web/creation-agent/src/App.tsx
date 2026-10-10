import { useEffect, useRef, useState } from 'react'
import { useStore } from 'zustand'
import type { Faults, Part } from './model.js'
import { agentStore } from './store.js'
import { MessageList } from './MessageList.js'
import { Composer } from './Composer.js'
export function App() {
  const mode = useStore(agentStore, (s) => s.mode)
  const status = useStore(agentStore, (s) => s.status)
  const faults = useStore(agentStore, (s) => s.faults)
  const toast = useStore(agentStore, (s) => s.toast)
  const [preview, setPreview] = useState<Extract<Part, { kind: 'artifact' }> | null>(null)
  const dialog = useRef<HTMLDialogElement>(null)
  const trigger = useRef<HTMLElement | null>(null)
  useEffect(() => {
    if (preview) {
      dialog.current?.showModal()
    }
  }, [preview])
  useEffect(() => {
    if (!toast) {
      return
    }
    const timer = setTimeout(() => agentStore.getState().dismissToast(), 5000)
    return () => clearTimeout(timer)
  }, [toast])
  useEffect(() => () => agentStore.getState().stop(), [])
  return (
    <div className="app-shell">
      <header>
        <a className="brand" href="/">
          ✦{' '}
          <span>
            帧屿集 <small>创作 Agent</small>
          </span>
        </a>
        <div className="session-title">新的创作会话</div>
        <span className="session-indicator">
          {
            {
              idle: '准备就绪',
              sending: '正在连接',
              streaming: '创作中',
              awaiting_user: '等待确认',
            }[status]
          }
        </span>
      </header>
      <div className="demo-bar">
        <label className="demo-label">
          对话来源{' '}
          <select
            aria-label="对话来源"
            value={mode}
            disabled={status !== 'idle'}
            onChange={(e) =>
              agentStore.getState().setMode(e.target.value === 'mock' ? 'mock' : 'server')
            }
          >
            <option value="server">服务端 Host</option>
            <option value="mock">本地演示</option>
          </select>
        </label>
        <button
          disabled={status !== 'idle'}
          onClick={() => agentStore.getState().send('生成一张咖啡海报', false, 'mock')}
        >
          跑 P0 演示
        </button>
        <button
          disabled={status !== 'idle'}
          onClick={() => agentStore.getState().send('做一条30秒咖啡广告，3个镜头', true, 'mock')}
        >
          跑 P1 演示
        </button>
        <details>
          <summary>故障模拟</summary>
          <div className="faults">
            {(
              [
                ['connection', '连接失败'],
                ['tool', '工具失败'],
                ['resume', '下次恢复失败'],
              ] as [keyof Faults, string][]
            ).map(([key, label]) => (
              <label key={key}>
                <input
                  type="checkbox"
                  checked={faults[key]}
                  disabled={key !== 'resume' && status !== 'idle'}
                  onChange={(e) => agentStore.getState().setFault(key, e.target.checked)}
                />
                {label}
              </label>
            ))}
          </div>
        </details>
      </div>
      <MessageList
        open={(part, element) => {
          trigger.current = element
          setPreview(part)
        }}
      />
      <Composer />
      {toast && (
        <div className="toast" role="alert">
          {toast}
          <button aria-label="关闭提示" onClick={() => agentStore.getState().dismissToast()}>
            ×
          </button>
        </div>
      )}
      <dialog
        ref={dialog}
        className="lightbox"
        aria-label="作品预览"
        onClick={(event) => {
          if (event.target === dialog.current) {
            dialog.current.close()
          }
        }}
        onClose={() => {
          setPreview(null)
          trigger.current?.focus()
        }}
      >
        <button aria-label="关闭作品预览" autoFocus onClick={() => dialog.current?.close()}>
          ×
        </button>
        {preview?.type === 'image' ? (
          <img src={preview.url} alt="创作作品大图" />
        ) : (
          preview && <video src={preview.url} controls />
        )}
      </dialog>
    </div>
  )
}
