import { emptyTimeline } from './state/timeline.js'
import { cmdAddObjects } from './state/commands.js'
import { defaultDraft } from './state/node-model.js'
import type { CanvasView } from './core/canvas-view.js'
import type { CanvasTool } from './core/types.js'

/** Canvas content rail; viewport navigation remains in the zoom controls. */
export function ContentToolbar({
  onImage,
  onVideo,
  onTimeline,
  onLibrary,
  onBoard,
  onText,
}: {
  onImage: () => void
  onVideo: () => void
  onTimeline: () => void
  onLibrary: () => void
  onBoard: () => void
  onText: () => void
}) {
  const tools = [
    {
      label: '图片',
      id: 'tool-upload',
      action: onImage,
      path: 'M3 4h18v16H3ZM7 8h.01M3 16l6-6 5 5 3-3 4 4',
    },
    {
      label: '视频',
      id: 'tool-video',
      action: onVideo,
      path: 'M3 5h13v14H3ZM16 10l5-3v10l-5-3',
    },
    {
      label: '时间线',
      id: 'tool-timeline',
      action: onTimeline,
      path: 'M4 4v16M8 6h12v4H8ZM8 14h8v4H8Z',
    },
    {
      label: '素材库',
      id: 'library-open',
      action: onLibrary,
      path: 'M3 7h7l2 2h9v11H3ZM3 7V4h7l2 3h7v2',
    },
    {
      label: '画板',
      id: 'tool-board',
      action: onBoard,
      path: 'M5 5h14a2 2 0 012 2v10a2 2 0 01-2 2H5a2 2 0 01-2-2V7a2 2 0 012-2Z',
    },
    { label: '文字', id: 'tool-text', action: onText, path: 'M4 5h16M12 5v15M8 20h8M4 5v3m16-3v3' },
  ]
  return (
    <nav className="tool-rail" aria-label="添加内容">
      {tools.map(({ label, id, action, path }) => (
        <button
          className="rail-btn"
          id={id}
          key={id}
          type="button"
          aria-label={label}
          onClick={action}
        >
          <svg
            className="rail-icon"
            width="20"
            height="20"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <path d={path} />
          </svg>
          <span className="rail-label">{label}</span>
        </button>
      ))}
    </nav>
  )
}

export function ViewportControls({
  view,
  tool,
  scale,
}: {
  view: CanvasView
  tool: CanvasTool
  scale: number
}) {
  const pan = tool === 'pan'
  return (
    <nav className="zoom-controls" aria-label="缩放控制">
      <button
        className="icon-btn"
        type="button"
        aria-label={pan ? '切换为选择工具' : '切换为拖动工具'}
        aria-pressed={pan}
        onClick={() => view.activateTool(pan ? 'select' : 'pan')}
      >
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path
            d={
              pan
                ? 'M8 12V6a2 2 0 014 0v6M12 6V4a2 2 0 014 0v8M16 8a2 2 0 014 0v7c0 4-3 7-7 7-3 0-5-2-7-5l-3-4a2 2 0 013-2l2 2'
                : 'm5 3 14 9-7 1-3 7Z'
            }
          />
        </svg>
      </button>
      <details className="zoom-menu">
        <summary className="canvas-tooltip" aria-label="缩放比例">
          <span className="zoom-value">{Math.round(scale * 100)}%</span>
        </summary>
        <div className="zoom-options">
          <button type="button" onClick={() => view.fitToContent()}>
            适应画布
          </button>
          {[0.5, 1, 2].map((value) => (
            <button key={value} type="button" onClick={() => view.zoomTo(value)}>
              {value * 100}%
            </button>
          ))}
        </div>
      </details>
    </nav>
  )
}

export function insertCanvasPlaceholder(view: CanvasView, type: 'image' | 'video' | 'timeline') {
  const viewport = view.getSnapshot().viewport
  const center = view.screenToWorld({ x: viewport.width / 2, y: viewport.height / 2 })
  const width = type === 'image' ? 320 : 480
  const height = type === 'image' ? 320 : type === 'video' ? 270 : 160
  const id = `placeholder-${crypto.randomUUID()}`
  view.execute(
    cmdAddObjects([
      {
        id,
        kind: 'placeholder',
        placeholderType: type,
        ...(type === 'timeline'
          ? { timeline: emptyTimeline() }
          : { mediaType: type, nodeDraft: defaultDraft(type) }),
        name: type === 'image' ? '图片占位符' : type === 'video' ? '视频占位符' : '时间线占位符',
        x: center.x - width / 2,
        y: center.y - height / 2,
        width,
        height,
      },
    ]),
  )
  view.select([id])
  view.activateTool('select')
}
