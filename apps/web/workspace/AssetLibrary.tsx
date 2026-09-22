import { useEffect, useRef, useState } from 'react'
import { Download, Upload } from 'lucide-react'
import { Button, Dialog, EmptyState, SearchInput, Select, Tabs } from '../ui/primitives.js'
import { CardMenu, LibraryHeader, LibraryToolbar, MediaPreview } from '../ui/library.js'
import { getAssets, request, type AssetEntry } from './api.js'
import { assetName, filterAssets, uploadFormat } from './library-model.js'

export function AssetLibrary({ onCreate }: { onCreate: () => void }) {
  const [assets, setAssets] = useState<AssetEntry[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [revision, setRevision] = useState(0)
  const [query, setQuery] = useState('')
  const [kind, setKind] = useState('all')
  const [sort, setSort] = useState('newest')
  const [selected, setSelected] = useState<AssetEntry | null>(null)
  const [uploading, setUploading] = useState(false)
  const [uploadMessage, setUploadMessage] = useState('')
  const [uploadError, setUploadError] = useState('')
  const input = useRef<HTMLInputElement>(null)
  const uploadController = useRef<AbortController | null>(null)
  useEffect(() => () => uploadController.current?.abort(), [])
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError('')
    void getAssets(controller.signal)
      .then((body) => {
        if (!controller.signal.aborted) {
          setAssets(body.assets)
          setLoading(false)
        }
      })
      .catch((err) => {
        if (!controller.signal.aborted) {
          setError(String(err))
          setLoading(false)
        }
      })
    return () => controller.abort()
  }, [revision])
  async function upload(files: File[]) {
    if (uploadController.current || !files.length) {
      return
    }
    const controller = new AbortController()
    uploadController.current = controller
    setUploading(true)
    setUploadError('')
    setUploadMessage('正在上传…')
    let completed = 0
    try {
      // Validate the whole selection before starting any writes.
      const formats = files.map(uploadFormat)
      for (const [index, file] of files.entries()) {
        setUploadMessage(`正在上传 ${index + 1} / ${files.length}：${file.name}`)
        const format = formats[index]!
        await request<AssetEntry>(
          `/api/assets?${new URLSearchParams({ ...format, name: file.name })}`,
          {
            method: 'POST',
            body: file,
            headers: { 'Content-Type': file.type },
            signal: controller.signal,
          },
        )
        completed++
      }
      if (!controller.signal.aborted) {
        setUploadMessage(`已上传 ${completed} 个素材`)
        setQuery('')
        setKind('all')
        setSort('newest')
      }
    } catch (err) {
      if (!controller.signal.aborted) {
        setUploadMessage('')
        setUploadError(
          `${err instanceof Error ? err.message : '上传失败'}。已完成 ${completed} 个；若网络中断，请先刷新确认结果，再上传未完成的文件。`,
        )
      }
    } finally {
      if (!controller.signal.aborted) {
        setUploading(false)
        setRevision((value) => value + 1)
      }
      uploadController.current = null
    }
  }
  const visible = filterAssets(assets, kind, query, sort)
  return (
    <section className="library-page asset-library">
      <LibraryHeader
        title="我的素材"
        description="收藏创作中的每一份可能。"
        action={
          <Button variant="primary" disabled={uploading} onClick={() => input.current?.click()}>
            <Upload size={22} />
            {uploading ? '上传中…' : '上传素材'}
          </Button>
        }
      />
      <input
        ref={input}
        type="file"
        hidden
        multiple
        accept="image/jpeg,image/png,image/webp,video/mp4,video/webm"
        onChange={(event) => {
          const files = Array.from(event.target.files || [])
          event.target.value = ''
          void upload(files)
        }}
      />
      <LibraryToolbar
        actions={
          <>
            <SearchInput label="搜索素材" value={query} onChange={setQuery} />
            <Select
              label="素材排序"
              value={sort}
              onChange={setSort}
              options={[
                { value: 'newest', label: '最新上传' },
                { value: 'oldest', label: '最早上传' },
              ]}
            />
          </>
        }
      >
        <Tabs
          label="素材类型"
          value={kind}
          onChange={setKind}
          options={[
            { value: 'all', label: '全部' },
            { value: 'image', label: '图片' },
            { value: 'video', label: '视频' },
          ]}
        />
      </LibraryToolbar>
      {uploadMessage && (
        <p role="status" className="library-status">
          {uploadMessage}
        </p>
      )}
      {uploadError && (
        <p role="alert" className="inline-error">
          {uploadError}
        </p>
      )}
      {loading ? (
        <p role="status">正在读取素材…</p>
      ) : error ? (
        <EmptyState
          action={<Button onClick={() => setRevision((value) => value + 1)}>重试</Button>}
        >
          {error}
        </EmptyState>
      ) : !visible.length ? (
        <EmptyState
          action={
            assets.length ? (
              <Button
                onClick={() => {
                  setQuery('')
                  setKind('all')
                }}
              >
                清除筛选
              </Button>
            ) : (
              <Button onClick={onCreate}>开始创作</Button>
            )
          }
        >
          {assets.length ? '没有找到匹配的素材' : '还没有素材，可以上传文件或开始创作。'}
        </EmptyState>
      ) : (
        <div className="asset-collection">
          {visible.map((item) => (
            <article className="asset-tile" key={item.asset.id}>
              <button
                className="library-preview-button"
                onClick={() => setSelected(item)}
                aria-label={`预览 ${assetName(item)}`}
              >
                <MediaPreview
                  src={
                    item.asset.kind === 'video'
                      ? item.urls.original
                      : item.urls.thumb1024 || item.urls.original
                  }
                  kind={item.asset.kind}
                  label={assetName(item)}
                />
              </button>
              <CardMenu
                label={`${assetName(item)}的操作`}
                actions={[
                  { label: '预览素材', onClick: () => setSelected(item) },
                  {
                    label: '下载素材',
                    onClick: () => {
                      const link = document.createElement('a')
                      link.href = item.urls.original
                      link.download = assetName(item)
                      link.click()
                    },
                  },
                ]}
              />
              <div className="library-card-copy">
                <h3 title={assetName(item)}>{assetName(item)}</h3>
                <p>
                  {item.asset.width > 0 && item.asset.height > 0
                    ? `${item.asset.width} × ${item.asset.height} · `
                    : ''}
                  {item.asset.ext.toUpperCase()}
                </p>
              </div>
            </article>
          ))}
        </div>
      )}
      <div className="library-footer">
        <span>{loading ? '' : `显示 ${visible.length} 项 · 最近 200 项素材`}</span>
        <Button disabled={loading || uploading} onClick={() => setRevision((value) => value + 1)}>
          刷新素材
        </Button>
      </div>
      {selected && (
        <Dialog title={assetName(selected)} onClose={() => setSelected(null)}>
          {selected.asset.kind === 'video' ? (
            <video
              className="library-detail"
              src={selected.urls.original}
              controls
              autoPlay
              playsInline
            />
          ) : (
            <img
              className="library-detail"
              src={selected.urls.original}
              alt={assetName(selected)}
            />
          )}
          <div className="dialog-actions">
            <a className="ui-button" href={selected.urls.original} download={assetName(selected)}>
              <Download size={18} />
              下载原文件
            </a>
          </div>
        </Dialog>
      )}
    </section>
  )
}
