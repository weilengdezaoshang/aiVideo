import { useEffect, useRef, useState } from 'react'
import type { CanvasObj } from './state/commands.js'
import { mediaSource } from './state/studio-bindings.js'

/** A shared image/video viewer; generation input stays beside the full frame. */
export function MediaPreview({ object, onClose }: { object: CanvasObj; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const returnFocus = useRef(document.activeElement)
  const [failed, setFailed] = useState(false)
  const [dimensions, setDimensions] = useState<string | null>(null)
  const [duration, setDuration] = useState<number | undefined>(object.durationSec)
  const source = mediaSource(object)
  const draft = object.nodeRun?.snapshot ?? object.nodeDraft
  const params = object.generationSpec?.params
  const prompt = typeof params?.prompt === 'string' ? params.prompt : draft?.prompt
  const model = typeof params?.model === 'string' ? params.model : draft?.model
  const promptLabel = params || object.nodeRun ? '生成提示词' : '节点提示词'
  useEffect(() => {
    const trigger = returnFocus.current
    dialog.current?.showModal()
    return () => {
      if (trigger instanceof HTMLElement && trigger.isConnected) {
        trigger.focus()
      }
    }
  }, [])
  const type = object.kind === 'video' ? '视频' : object.kind === 'text' ? '文字' : '图片'
  return (
    <dialog
      ref={dialog}
      className="selection-preview"
      aria-label="媒体全屏预览"
      onCancel={onClose}
      onClose={onClose}
      onClick={(event) => {
        if (event.target === dialog.current) {
          onClose()
        }
      }}
    >
      <div className="media-preview-shell">
        <header className="media-preview-header">
          <span>{type}预览</span>
          {source && !failed && object.kind !== 'text' && (
            <a className="media-preview-mobile-download" href={source} download>
              下载{type} ↓
            </a>
          )}
          <button type="button" autoFocus onClick={onClose}>
            关闭预览 <span aria-hidden="true">×</span>
          </button>
        </header>
        <div className="media-preview-layout">
          <div className="media-preview-stage">
            {failed ? (
              <p role="alert">{type}加载失败，请关闭后重试。</p>
            ) : object.kind === 'text' ? (
              <p>{object.text}</p>
            ) : !source ? (
              <p>暂无可预览的媒体</p>
            ) : object.kind === 'video' ? (
              <video
                src={source}
                controls
                playsInline
                preload="metadata"
                onError={() => setFailed(true)}
                onLoadedMetadata={(event) => {
                  const video = event.currentTarget
                  setDimensions(`${video.videoWidth} × ${video.videoHeight}`)
                  if (Number.isFinite(video.duration)) {
                    setDuration(video.duration)
                  }
                }}
              />
            ) : (
              <img
                src={source}
                alt={object.name || '图片预览'}
                onError={() => setFailed(true)}
                onLoad={(event) => {
                  const image = event.currentTarget
                  setDimensions(`${image.naturalWidth} × ${image.naturalHeight}`)
                }}
              />
            )}
          </div>
          <aside className="media-preview-info">
            <span className="media-preview-eyebrow">画布作品</span>
            <h2>{object.name || `${type}作品`}</h2>
            <dl>
              <div>
                <dt>类型</dt>
                <dd>{type}</dd>
              </div>
              {dimensions && (
                <div>
                  <dt>尺寸</dt>
                  <dd>{dimensions}</dd>
                </div>
              )}
              {object.kind === 'video' && duration !== undefined && (
                <div>
                  <dt>时长</dt>
                  <dd>{Math.round(duration * 10) / 10} 秒</dd>
                </div>
              )}
              {model && (
                <div>
                  <dt>模型</dt>
                  <dd>{model}</dd>
                </div>
              )}
            </dl>
            {prompt && (
              <section className="media-preview-prompt">
                <h3>{promptLabel}</h3>
                <p>{prompt}</p>
              </section>
            )}
            {source && !failed && object.kind !== 'text' && (
              <a className="media-preview-download" href={source} download>
                下载{type} <span aria-hidden="true">↓</span>
              </a>
            )}
          </aside>
        </div>
      </div>
    </dialog>
  )
}
