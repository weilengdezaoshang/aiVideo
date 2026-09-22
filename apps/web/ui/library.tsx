import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Ellipsis, Image as ImageIcon, LayoutGrid, List, Play } from 'lucide-react'
import { Button } from './primitives.js'

export function LibraryHeader({
  title,
  description,
  count,
  action,
}: {
  title: string
  description: string
  count?: string
  action: ReactNode
}) {
  return (
    <header className="library-header">
      <div>
        <h1>{title}</h1>
        <p>{description}</p>
        {count && <span className="library-count">{count}</span>}
      </div>
      {action}
    </header>
  )
}

export function LibraryToolbar({ children, actions }: { children: ReactNode; actions: ReactNode }) {
  return (
    <div className="library-toolbar">
      <div className="library-toolbar-main">{children}</div>
      <div className="library-toolbar-actions">{actions}</div>
    </div>
  )
}

export function ViewToggle({
  value,
  onChange,
}: {
  value: 'grid' | 'list'
  onChange: (value: 'grid' | 'list') => void
}) {
  return (
    <div className="library-view-toggle" role="group" aria-label="画布显示方式">
      {(
        [
          { value: 'grid', label: '网格视图', icon: LayoutGrid },
          { value: 'list', label: '列表视图', icon: List },
        ] as const
      ).map(({ value: mode, label, icon: Icon }) => (
        <Button
          key={mode}
          aria-label={label}
          title={label}
          aria-pressed={value === mode}
          onClick={() => onChange(mode)}
        >
          <Icon size={21} />
        </Button>
      ))}
    </div>
  )
}

export function CardMenu({
  label,
  actions,
}: {
  label: string
  actions: { label: string; onClick: () => void; danger?: boolean }[]
}) {
  const ref = useRef<HTMLDetailsElement>(null)
  useEffect(() => {
    const close = (event: PointerEvent) => {
      if (event.target instanceof Node && !ref.current?.contains(event.target)) {
        ref.current?.removeAttribute('open')
      }
    }
    document.addEventListener('pointerdown', close)
    return () => document.removeEventListener('pointerdown', close)
  }, [])
  return (
    <details
      className="library-card-menu"
      ref={ref}
      onKeyDown={(event) => {
        if (event.key === 'Escape') {
          ref.current?.removeAttribute('open')
          ref.current?.querySelector('summary')?.focus()
        }
      }}
    >
      <summary aria-label={label}>
        <Ellipsis size={20} />
      </summary>
      <div>
        {actions.map((action) => (
          <Button
            key={action.label}
            variant={action.danger ? 'danger' : 'ghost'}
            onClick={() => {
              ref.current?.removeAttribute('open')
              action.onClick()
            }}
          >
            {action.label}
          </Button>
        ))}
      </div>
    </details>
  )
}

export function MediaPreview({
  src,
  kind = 'image',
  label,
  decorative = false,
}: {
  src?: string
  kind?: 'image' | 'video'
  label: string
  /** 相邻文本已提供名称时置 true:媒体转装饰性,避免链接可达名重复 */
  decorative?: boolean
}) {
  const [failed, setFailed] = useState(false)
  const [duration, setDuration] = useState('')
  useEffect(() => {
    setFailed(false)
    setDuration('')
  }, [src])
  return (
    <div className="library-media">
      {!src || failed ? (
        <ImageIcon size={48} strokeWidth={1.1} aria-hidden="true" />
      ) : kind === 'video' ? (
        <>
          <video
            src={src}
            muted
            playsInline
            preload="metadata"
            aria-label={decorative ? undefined : label}
            onError={() => setFailed(true)}
            onLoadedMetadata={(event) => {
              const seconds = event.currentTarget.duration
              if (Number.isFinite(seconds)) {
                setDuration(
                  `${Math.floor(seconds / 60)
                    .toString()
                    .padStart(2, '0')}:${Math.floor(seconds % 60)
                    .toString()
                    .padStart(2, '0')}`,
                )
              }
            }}
          />
          <span className="library-play">
            <Play size={18} fill="currentColor" />
          </span>
          {duration && <span className="library-duration">{duration}</span>}
        </>
      ) : (
        <img
          src={src}
          alt={decorative ? '' : label}
          loading="lazy"
          onError={() => setFailed(true)}
        />
      )}
    </div>
  )
}
