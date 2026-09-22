import { useEffect, useRef, useState } from 'react'
import { ArrowUpRight, ImagePlus, LoaderCircle, ScanLine, PencilLine, X } from 'lucide-react'
import { Button, IconButton, Select, Tabs } from '../ui/primitives.js'
import {
  defaultDraft,
  type Capabilities,
  type MediaKind,
  type NodeDraft,
  type Reference,
} from '../canvas/state/node-model.js'
import { uploadReference, validateGeneration } from './api.js'

export type PromptPreset = { prompt: string; ratio: string; version: number }
export function Composer({
  caps,
  capsError,
  onReload,
  preset,
  onGenerate,
  pending,
  busy,
  onTool,
}: {
  caps: Capabilities | null
  capsError: string
  onReload: () => void
  preset: PromptPreset | null
  onGenerate: (kind: MediaKind, draft: NodeDraft) => void
  pending: boolean
  busy: boolean
  onTool: (tool: string) => void
}) {
  const [kind, setKind] = useState<MediaKind>('image')
  const [prompt, setPrompt] = useState('')
  const [model, setModel] = useState('')
  const [ratio, setRatio] = useState('16:9')
  const [duration, setDuration] = useState('4')
  const [reference, setReference] = useState<(Reference & { url: string }) | null>(null)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState('')
  const [dragging, setDragging] = useState(false)
  const input = useRef<HTMLInputElement>(null)
  const textarea = useRef<HTMLTextAreaElement>(null)
  const uploadController = useRef<AbortController | null>(null)
  const cap = caps?.[kind]
  const selectedModel = cap?.models.some((item) => item.id === model)
    ? model
    : cap?.models[0]?.id || ''
  const selectedRatio = cap?.ratios.includes(ratio) ? ratio : cap?.ratios[0] || '16:9'
  const selectedDuration = cap?.durations?.includes(Number(duration))
    ? Number(duration)
    : cap?.durations?.[0] || 4
  useEffect(() => () => uploadController.current?.abort(), [])
  useEffect(() => {
    if (preset) {
      setPrompt(preset.prompt)
      setRatio(preset.ratio)
      setKind('image')
      setError('')
      textarea.current?.focus()
    }
  }, [preset])
  async function upload(file?: File) {
    if (!file || pending || busy) {
      return
    }
    uploadController.current?.abort()
    const controller = new AbortController()
    uploadController.current = controller
    setUploading(true)
    setError('')
    try {
      const result = await uploadReference(file, controller.signal)
      if (!controller.signal.aborted) {
        setReference(result)
      }
    } catch (err) {
      if (!controller.signal.aborted) {
        setError(err instanceof Error ? err.message : '上传失败')
      }
    } finally {
      if (!controller.signal.aborted) {
        setUploading(false)
      }
    }
  }
  const draft: NodeDraft = {
    ...defaultDraft(kind),
    prompt,
    model: selectedModel,
    ratio: selectedRatio,
    resolution: cap?.resolutions.includes(1024) ? 1024 : cap?.resolutions[0] || 512,
    durationSec: selectedDuration,
    references: reference
      ? [{ assetId: reference.assetId, ext: reference.ext, name: reference.name }]
      : [],
  }
  const validation = caps ? validateGeneration(kind, draft, caps) : '正在读取模型'
  return (
    <section className="creation-section" aria-label="开始创作">
      <header className="creation-heading">
        <h1>今天，想创造什么？</h1>
        <p>从一张图，到一个故事。</p>
      </header>
      <form
        className={`composer ${dragging ? 'is-dragging' : ''}`}
        aria-busy={busy || uploading}
        onSubmit={(event) => {
          event.preventDefault()
          if (!pending && validation) {
            setError(validation)
            return
          }
          onGenerate(kind, draft)
        }}
        onDragOver={(event) => {
          event.preventDefault()
          if (!pending) {
            setDragging(true)
          }
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault()
          setDragging(false)
          if (cap?.referenceLimit) {
            void upload(event.dataTransfer.files[0])
          } else {
            setError('当前模型不支持参考图')
          }
        }}
      >
        <Tabs
          label="生成方式"
          value={kind}
          onChange={(value) => {
            setKind(value)
            setError('')
          }}
          disabled={pending || busy || uploading}
          options={[
            { value: 'image', label: '图片生成' },
            { value: 'video', label: '图生视频' },
          ]}
        />
        <textarea
          id="creation-prompt"
          ref={textarea}
          aria-label="创作提示词"
          placeholder="描述你想创作的画面，或添加一张参考图…"
          value={prompt}
          onChange={(event) => {
            setPrompt(event.target.value)
            setError('')
          }}
          maxLength={4000}
          disabled={pending || busy}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
              event.preventDefault()
              event.currentTarget.form?.requestSubmit()
            }
          }}
        />
        {reference && (
          <div className="reference-preview">
            <img src={reference.url} alt="已上传的参考图" />
            <span>{reference.name}</span>
            <IconButton
              label="移除参考图"
              disabled={pending || busy || uploading}
              onClick={() => setReference(null)}
            >
              <X size={16} />
            </IconButton>
          </div>
        )}
        <div className="composer-toolbar">
          <input
            ref={input}
            type="file"
            accept="image/jpeg,image/png,image/webp"
            hidden
            onChange={(event) => {
              void upload(event.target.files?.[0])
              event.target.value = ''
            }}
          />
          <Button
            disabled={!cap?.referenceLimit || !cap.supported || pending || busy || uploading}
            onClick={() => input.current?.click()}
            title="上传 8MB 以内的 JPG、PNG 或 WebP，也可拖入图片"
          >
            {uploading ? (
              <LoaderCircle size={21} className="spinning" />
            ) : (
              <ImagePlus size={22} strokeWidth={1.5} />
            )}
            <span>{uploading ? '上传中…' : '上传参考图'}</span>
          </Button>
          <span className="toolbar-divider" />
          <Select
            label="模型"
            value={selectedModel}
            disabled={pending || busy || !cap?.supported}
            onChange={setModel}
            options={
              cap?.models.length
                ? cap.models.map((item) => ({ value: item.id, label: item.name }))
                : [{ value: '', label: caps ? '暂无可用模型' : '加载模型…' }]
            }
          />
          <span className="toolbar-divider" />
          <Select
            label="画面比例"
            value={selectedRatio}
            onChange={setRatio}
            disabled={pending || busy || !cap?.supported}
            options={(cap?.ratios || ['16:9']).map((value) => ({ value, label: value }))}
          />
          {kind === 'video' && (
            <Select
              label="视频时长"
              value={String(selectedDuration)}
              onChange={setDuration}
              disabled={pending || busy || !cap?.supported}
              options={(cap?.durations || [4]).map((value) => ({
                value: String(value),
                label: `${value} 秒`,
              }))}
            />
          )}
          <Button
            type="submit"
            variant="primary"
            className="generate-button"
            disabled={busy || uploading || (!pending && !!validation)}
            title={validation || '开始生成（⌘ / Ctrl + Enter）'}
          >
            {busy ? <LoaderCircle className="spinning" size={18} /> : null}
            {busy ? '提交中…' : pending ? '重试同一请求' : '生成'}
            <ArrowUpRight size={21} />
          </Button>
        </div>
        {capsError && (
          <div role="alert" className="inline-error">
            模型读取失败：{capsError}
            <Button onClick={onReload}>重新加载</Button>
          </div>
        )}
        {(error ||
          (cap && !cap.supported && cap.reason) ||
          (kind === 'video' &&
            !reference &&
            cap?.supported &&
            '上传一张图片，作为视频的首帧。')) && (
          <p className={error ? 'inline-error' : 'composer-note'} role={error ? 'alert' : 'status'}>
            {error || (!cap?.supported ? cap?.reason : '上传一张图片，作为视频的首帧。')}
          </p>
        )}
      </form>
      <div className="image-tools">
        <span>图片工具</span>
        <i />
        <Button onClick={() => onTool('局部重绘')}>
          <PencilLine size={16} />
          局部重绘
          <ArrowUpRight size={15} />
        </Button>
        <i />
        <Button onClick={() => onTool('智能抠图')}>
          <ScanLine size={16} />
          智能抠图
          <ArrowUpRight size={15} />
        </Button>
      </div>
    </section>
  )
}
