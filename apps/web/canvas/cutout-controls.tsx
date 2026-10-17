import { useEffect, useState, type CSSProperties } from 'react'
import type { MaskBrush } from './core/types.js'

export interface CutoutControlsProps {
  phase: 'detecting' | 'ready' | 'error' | 'applying'
  error: string | null
  strokeCount: number
  showMask: boolean
  brush: MaskBrush
  canRedo: boolean
  style?: CSSProperties
  settingsPlacement?: 'above' | 'below'
  onBrushChange: (brush: MaskBrush) => void
  onUndo: () => void
  onRedo: () => void
  onToggleMask: () => void
  onReset: () => void
  onRetry: () => void
  onApply: () => void
  onCancel: () => void
}

function Icon({
  name,
}: {
  name: 'paint' | 'erase' | 'recognize' | 'undo' | 'redo' | 'compare' | 'close' | 'size'
}) {
  const paths = {
    paint: 'm14 4 6 6M4 20l4-1L20 7a2 2 0 0 0-4-4L4 15v5Z',
    erase: 'm4 13 8-9a2 2 0 0 1 3 0l6 6a2 2 0 0 1 0 3l-7 8H9l-5-4a2 2 0 0 1 0-3Zm4-5 9 9M14 20h7',
    recognize: 'M12 3v4m0 10v4M3 12h4m10 0h4M5 5l3 3m8 8 3 3M5 19l3-3m8-8 3-3',
    undo: 'M9 5 4 10l5 5M4 10h10a6 6 0 0 1 6 6v3',
    redo: 'm15 5 5 5-5 5m5-5H10a6 6 0 0 0-6 6v3',
    compare: 'M12 3v18M4 6h16v12H4V6Z',
    close: 'm6 6 12 12M6 18 18 6',
    size: 'M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16Z',
  }
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.65"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d={paths[name]} />
    </svg>
  )
}

/** Image-anchored tool strip: selection edits are local until explicit confirmation. */
export function CutoutControls(props: CutoutControlsProps) {
  const { phase, onCancel } = props
  const busy = phase === 'detecting' || phase === 'applying'
  const [settings, setSettings] = useState(false)
  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      const target = event.target
      const editing =
        target instanceof HTMLElement &&
        (target.isContentEditable || ['INPUT', 'TEXTAREA'].includes(target.tagName))
      if (
        !busy &&
        !editing &&
        (event.metaKey || event.ctrlKey) &&
        event.key.toLowerCase() === 'z'
      ) {
        event.preventDefault()
        event.stopPropagation()
        if (event.shiftKey) {
          props.onRedo()
        } else {
          props.onUndo()
        }
      }
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopPropagation()
        if (settings) {
          setSettings(false)
        } else if (phase !== 'applying') {
          onCancel()
        }
      }
    }
    window.addEventListener('keydown', escape, true)
    return () => window.removeEventListener('keydown', escape, true)
  }, [phase, onCancel, settings, busy, props.onRedo, props.onUndo])

  return (
    <section
      className="next-cutout-controls"
      aria-label="抠图工具"
      data-phase={phase}
      data-settings-placement={props.settingsPlacement ?? 'below'}
      style={props.style}
    >
      {busy ? (
        <>
          <div className="next-cutout-progress" role="status" aria-live="polite">
            <span className="next-cutout-spinner" aria-hidden="true" />
            {phase === 'detecting' ? '正在识别主体…' : '正在抠图…'}
          </div>
          <button
            type="button"
            className="next-cutout-icon"
            title="退出抠图"
            aria-label="退出抠图"
            disabled={phase === 'applying'}
            onClick={onCancel}
          >
            <Icon name="close" />
          </button>
        </>
      ) : (
        <>
          <div className="next-cutout-tools" role="group" aria-label="蒙版编辑">
            <button
              className="next-cutout-icon"
              type="button"
              title="画笔 · 保留区域"
              aria-label="画笔"
              aria-pressed={props.brush.mode === 'paint'}
              onClick={() => props.onBrushChange({ ...props.brush, mode: 'paint' })}
            >
              <Icon name="paint" />
            </button>
            <button
              className="next-cutout-icon"
              type="button"
              title="橡皮擦 · 移除区域"
              aria-label="橡皮擦"
              aria-pressed={props.brush.mode === 'erase'}
              onClick={() => props.onBrushChange({ ...props.brush, mode: 'erase' })}
            >
              <Icon name="erase" />
            </button>
            <button
              className="next-cutout-icon"
              type="button"
              title="笔刷大小"
              aria-label="笔刷大小"
              aria-expanded={settings}
              aria-controls="cutout-brush-size"
              onClick={() => setSettings(!settings)}
            >
              <Icon name="size" />
            </button>
            <button
              className="next-cutout-icon"
              type="button"
              title="重新识别主体（重置修正）"
              aria-label="重新识别"
              onClick={props.onRetry}
            >
              <Icon name="recognize" />
            </button>
          </div>
          <span className="next-cutout-separator" />
          <div className="next-cutout-tools" role="group" aria-label="笔画历史">
            <button
              className="next-cutout-icon"
              type="button"
              title="撤销笔画"
              aria-label="撤销笔画"
              disabled={!props.strokeCount}
              onClick={props.onUndo}
            >
              <Icon name="undo" />
            </button>
            <button
              className="next-cutout-icon"
              type="button"
              title="重做笔画"
              aria-label="重做笔画"
              disabled={!props.canRedo}
              onClick={props.onRedo}
            >
              <Icon name="redo" />
            </button>
          </div>
          <span className="next-cutout-separator" />
          <button
            className="next-cutout-icon"
            type="button"
            title="对照原图"
            aria-label="对照原图"
            aria-pressed={!props.showMask}
            onClick={props.onToggleMask}
          >
            <Icon name="compare" />
          </button>
          <button
            className="next-cutout-confirm"
            type="button"
            aria-label="确认抠图"
            disabled={phase === 'error' && !props.strokeCount}
            onClick={props.onApply}
          >
            抠图
          </button>
          <button
            className="next-cutout-icon"
            type="button"
            title="退出抠图 · Esc"
            aria-label="退出抠图"
            onClick={onCancel}
          >
            <Icon name="close" />
          </button>
          {settings && (
            <div className="next-cutout-settings" id="cutout-brush-size">
              <label htmlFor="cutout-size">
                大小 <output>{props.brush.size}</output>
              </label>
              <input
                id="cutout-size"
                aria-label="笔刷大小"
                type="range"
                min="4"
                max="120"
                step="1"
                value={props.brush.size}
                onChange={(event) =>
                  props.onBrushChange({ ...props.brush, size: Number(event.target.value) })
                }
              />
              <button type="button" onClick={props.onReset} disabled={!props.strokeCount}>
                清空笔画
              </button>
            </div>
          )}
        </>
      )}
      {props.error && (
        <div className="next-cutout-error" role="alert">
          <span>{props.error}</span>
          {phase === 'error' && (
            <button type="button" onClick={props.onRetry}>
              重新识别
            </button>
          )}
        </div>
      )}
    </section>
  )
}
