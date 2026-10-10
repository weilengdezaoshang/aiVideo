import { useEffect, useRef, useState } from 'react'
import { defaultVectorParams, vectorize } from './vector.js'
import { objectToDataUrl } from './flows/asset-util.js'
import type { CanvasObj } from './state/commands.js'

export function SvgStudio({ object, onClose }: { object: CanvasObj; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [params, setParams] = useState(defaultVectorParams)
  const [svg, setSvg] = useState('')
  const [preview, setPreview] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    dialog.current?.showModal()
  }, [])
  useEffect(() => {
    const controller = new AbortController()
    setBusy(true)
    setError('')
    setSvg('')
    const timer = setTimeout(() => {
      void objectToDataUrl(object)
        .then(async (src) => {
          const image = new Image()
          image.src = src
          await image.decode()
          return vectorize({ image, params, signal: controller.signal })
        })
        .then((value) => {
          if (!controller.signal.aborted) {
            setSvg(value)
          }
        })
        .catch((cause: unknown) => {
          if (!controller.signal.aborted) {
            setError(cause instanceof Error ? cause.message : '转换失败')
          }
        })
        .finally(() => {
          if (!controller.signal.aborted) {
            setBusy(false)
          }
        })
    }, 160)
    return () => {
      clearTimeout(timer)
      controller.abort()
    }
  }, [object.id, object.src, object.assetId, params])
  useEffect(() => {
    if (!svg) {
      setPreview('')
      return
    }
    const url = URL.createObjectURL(new Blob([svg], { type: 'image/svg+xml' }))
    setPreview(url)
    return () => URL.revokeObjectURL(url)
  }, [svg])
  return (
    <dialog className="studio-svg-dialog" ref={dialog} aria-label="SVG 导出" onCancel={onClose}>
      <h2>SVG 导出</h2>
      {preview && <img src={preview} alt="矢量化预览" />}
      {(['colors', 'detail', 'simplify'] as const).map((key) => (
        <label key={key}>
          {{ colors: '颜色层数', detail: '细节程度', simplify: '简化程度' }[key]}：{params[key]}
          <input
            type="range"
            min={key === 'colors' ? 2 : 0}
            max={key === 'colors' ? 16 : key === 'detail' ? 10 : 64}
            value={params[key]}
            onChange={(event) =>
              setParams((current) => ({ ...current, [key]: Number(event.target.value) }))
            }
          />
        </label>
      ))}
      <p role="status">{busy ? '正在转换…' : error}</p>
      {preview && !busy && (
        <a href={preview} download={`${object.name || '画布图片'}.svg`}>
          下载 SVG
        </a>
      )}
      <button type="button" onClick={onClose}>
        关闭
      </button>
    </dialog>
  )
}
