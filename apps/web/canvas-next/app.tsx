import { installSessionFetch } from '../shared/session-fetch.js'
installSessionFetch()

import { StrictMode, useEffect, useMemo, useRef, useState, type ButtonHTMLAttributes } from 'react'
import { createRoot } from 'react-dom/client'

import { CanvasEditor } from '../canvas/renderer-konva/index.js'
import {
  CanvasViewProvider,
  useCanvasView,
  useCanvasViewSnapshot,
} from '../canvas/react/canvas-view-context.js'
import { createAutoSaver, type SaveStatus } from '../canvas/state/autosave.js'
import { cmdAddObjects, cmdUpdateObject } from '../canvas/state/commands.js'
import { connectReference } from '../canvas/flows/connect-reference.js'
import { getGenerationCoordinator, submitGeneration } from '../canvas/flows/generate.js'
import { ensureAsset, objectToDataUrl } from '../canvas/flows/asset-util.js'
import { connectGenerationEvents } from '../canvas/state/generation-events.js'
import { RecognitionOperation } from '../canvas/flows/recognition-operation.js'
import type { CanvasHitTarget, Point, Rect } from '../canvas/core/types.js'
import { openDocument } from '../canvas/state/document-api.js'
import { createDocStore, type CanvasDocData, type DocStore } from '../canvas/state/doc-store.js'
import {
  defaultDraft,
  migrateNodes,
  outputSize,
  type Capabilities,
  type MediaKind,
} from '../canvas/state/node-model.js'
import { emptyStoryboard, newShot, type Storyboard } from '../canvas/state/storyboard.js'
import { emptyTimeline, moveClip, type Timeline } from '../canvas/state/timeline.js'
type LoadedCanvas = { document: CanvasDocData; store: DocStore }
type TextEditorState = { objectId: string; rect: Rect; value: string }
type ContextMenuState = { target: CanvasHitTarget | null; point: Point }
type InpaintRequest = { sourceId: string; region: Rect; prompt: string }

function CanvasNextApp() {
  const [loaded, setLoaded] = useState<LoadedCanvas | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    void openDocument()
      .then((document) => {
        if (!active) {
          return
        }
        migrateNodes(document.objects)
        setLoaded({ document, store: createDocStore(document) })
      })
      .catch((cause: unknown) => {
        if (active) {
          setError(cause instanceof Error ? cause.message : '画布文档读取失败')
        }
      })
    return () => {
      active = false
    }
  }, [])

  if (error) {
    return <main className="next-state next-error">{error}</main>
  }
  if (!loaded) {
    return <main className="next-state">正在打开画布…</main>
  }

  return (
    <CanvasViewProvider store={loaded.store}>
      <CanvasShell document={loaded.document} store={loaded.store} />
    </CanvasViewProvider>
  )
}

function CanvasShell({ document, store }: LoadedCanvas) {
  const view = useCanvasView()
  const snapshot = useCanvasViewSnapshot()
  const [saveStatus, setSaveStatus] = useState<SaveStatus>('idle')
  const [notice, setNotice] = useState('')
  const [textEditor, setTextEditor] = useState<TextEditorState | null>(null)
  const [contextMenu, setContextMenu] = useState<ContextMenuState | null>(null)
  const [prompt, setPrompt] = useState('')
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null)
  const [generationKind, setGenerationKind] = useState<MediaKind>('image')
  const [generationDraft, setGenerationDraft] = useState(() => defaultDraft('image'))
  const [generating, setGenerating] = useState(false)
  const [connected, setConnected] = useState(true)
  const [inpaintRequest, setInpaintRequest] = useState<InpaintRequest | null>(null)
  const [maskSourceId, setMaskSourceId] = useState<string | null>(null)
  const [maskBaseUrl, setMaskBaseUrl] = useState<string | null>(null)
  const recognition = useRef<RecognitionOperation | null>(null)
  const [sideTab, setSideTab] = useState<
    'inspector' | 'library' | 'chat' | 'storyboard' | 'timeline'
  >('inspector')
  const fitted = useRef(false)
  const saver = useMemo(
    () =>
      createAutoSaver({
        docId: document.id,
        getDoc: () => store.doc,
        onStatus: setSaveStatus,
      }),
    [document.id, store],
  )
  const generationCoordinator = useMemo(() => getGenerationCoordinator(store), [store])

  useEffect(() => {
    return () => recognition.current?.cancel()
  }, [])
  useEffect(() => {
    const sourceId = maskSourceId
    if (sourceId && snapshot.selection.length > 0 && !snapshot.selection.includes(sourceId)) {
      recognition.current?.cancel()
      recognition.current = null
      setMaskSourceId(null)
      setMaskBaseUrl(null)
      view.clearMaskDraft()
      view.activateTool('select')
    }
  }, [maskSourceId, snapshot.selection, view])

  useEffect(
    () => store.subscribe((event) => event.type !== 'transient' && saver.markDirty()),
    [saver, store],
  )
  useEffect(() => {
    const unload = () => saver.flushOnUnload()
    window.addEventListener('beforeunload', unload)
    return () => window.removeEventListener('beforeunload', unload)
  }, [saver])
  useEffect(() => {
    const events = connectGenerationEvents({
      docStore: store,
      coordinator: generationCoordinator,
      onConnectionChange: setConnected,
    })
    return () => events.close()
  }, [generationCoordinator, store])
  useEffect(
    () =>
      generationCoordinator.runtime.subscribe(() => {
        store.mutateTransient(() => undefined)
      }),
    [generationCoordinator, store],
  )
  useEffect(() => {
    const controller = new AbortController()
    void fetch('/api/generation-capabilities', { signal: controller.signal })
      .then((response) => response.json())
      .then((body: Capabilities) => {
        if (!controller.signal.aborted) {
          setCapabilities(body)
          setGenerationDraft((draft) => ({ ...draft, model: body.image.models[0]?.id ?? '' }))
        }
      })
      .catch(() => {})
    return () => controller.abort()
  }, [])
  useEffect(
    () =>
      view.on('feedback:requested', ({ message }) => {
        setNotice(message)
        window.setTimeout(() => setNotice(''), 2200)
      }),
    [view],
  )
  useEffect(
    () =>
      view.on('connection:requested', ({ sourceId, targetId }) => {
        const controller = new AbortController()
        void connectReference(store, sourceId, targetId, controller.signal)
          .then(() => setNotice('引用连接已建立'))
          .catch((cause: unknown) => {
            setNotice(cause instanceof Error ? cause.message : '连接失败')
          })
      }),
    [store, view],
  )
  useEffect(
    () =>
      view.on('context-menu:requested', ({ target, screenPoint }) => {
        if (target?.kind === 'object') {
          view.select([target.objectId])
        }
        setContextMenu({ target, point: screenPoint })
      }),
    [view],
  )
  useEffect(
    () =>
      view.on('mask:stroke-committed', ({ sourceId, points }) => {
        setMaskSourceId(sourceId)
        setNotice(`已记录蒙版笔画（${points.length} 个采样点）`)
      }),
    [view],
  )
  useEffect(
    () =>
      view.on('inpaint:region-committed', ({ sourceId, region }) => {
        setInpaintRequest({ sourceId, region, prompt: '' })
      }),
    [view],
  )
  useEffect(
    () =>
      view.on('text-edit:requested', ({ objectId, screenRect }) => {
        const object = store.doc.objects[objectId]
        if (object?.kind === 'text') {
          setTextEditor({ objectId, rect: screenRect, value: object.text ?? '' })
        }
      }),
    [store, view],
  )
  useEffect(() => {
    if (fitted.current || snapshot.viewport.width <= 1 || snapshot.viewport.height <= 1) {
      return
    }
    fitted.current = true
    view.fitToContent()
  }, [snapshot.viewport.height, snapshot.viewport.width, view])

  return (
    <main className="next-shell">
      <header className="next-topbar">
        <a href="/workspace?tab=canvas" className="next-back">
          返回项目
        </a>
        <strong title={document.name}>{document.name}</strong>
        <span className="next-spacer" />
        <span className={`next-connection ${connected ? '' : 'is-offline'}`}>
          {connected ? '已连接' : '正在重连'}
        </span>
        <span className="next-status">{saveLabel(saveStatus)}</span>
      </header>
      <aside className="next-toolbar" aria-label="画布工具栏">
        <ToolButton
          active={snapshot.requestedTool === 'select'}
          onClick={() => view.activateTool('select')}
        >
          选择
        </ToolButton>
        <ToolButton
          active={snapshot.requestedTool === 'pan'}
          onClick={() => view.activateTool('pan')}
        >
          抓手
        </ToolButton>
        <ToolButton
          active={snapshot.requestedTool === 'connect'}
          onClick={() => view.activateTool('connect')}
        >
          连线
        </ToolButton>
        <ToolButton
          active={snapshot.requestedTool === 'text'}
          onClick={() => view.activateTool('text')}
        >
          文字
        </ToolButton>
        <ToolButton
          active={snapshot.requestedTool === 'mask'}
          disabled={!snapshot.capabilities.canUseMask}
          onClick={() => {
            const sourceId = snapshot.selection[0]
            const rawSource = sourceId ? store.doc.objects[sourceId] : undefined
            const source = sourceId ? view.getDocument().objects[sourceId] : undefined
            if (!source || source.kind !== 'image') {
              setNotice('请先选择一张图片')
              return
            }
            recognition.current?.cancel()
            setMaskSourceId(source.id)
            setMaskBaseUrl(null)
            view.clearMaskDraft()
            view.activateTool('recognizing')
            const operation = new RecognitionOperation(
              () => store.doc.objects[source.id] === rawSource && recognition.current === operation,
            )
            recognition.current = operation
            void ensureAsset(source, operation.signal)
              .then((asset) => operation.detect(asset.id))
              .then((url) => {
                operation.assertCurrent()
                setMaskBaseUrl(url)
                operation.complete()
                recognition.current = null
                view.activateTool('mask')
                setNotice('主体已识别，可继续涂抹修正')
              })
              .catch((cause: unknown) => {
                if (!(cause instanceof DOMException && cause.name === 'AbortError')) {
                  setNotice(cause instanceof Error ? cause.message : '主体识别失败')
                }
                if (recognition.current === operation) {
                  recognition.current = null
                  view.activateTool('select')
                }
              })
          }}
        >
          抠图
        </ToolButton>
        <ToolButton
          active={snapshot.requestedTool === 'inpaint'}
          disabled={!snapshot.capabilities.canUseMask}
          onClick={() => view.activateTool('inpaint')}
        >
          重绘
        </ToolButton>
        <span className="next-divider" />
        <ToolButton disabled={!snapshot.capabilities.canUndo} onClick={() => view.undo()}>
          撤销
        </ToolButton>
        <ToolButton disabled={!snapshot.capabilities.canRedo} onClick={() => view.redo()}>
          重做
        </ToolButton>
        <ToolButton
          disabled={!snapshot.capabilities.canDelete}
          onClick={() => view.deleteSelection()}
        >
          删除
        </ToolButton>
        <span className="next-divider" />
        <ToolButton onClick={() => view.zoomOut()}>−</ToolButton>
        <button className="next-scale" onClick={() => view.fitToContent()}>
          {Math.round(snapshot.camera.scale * 100)}%
        </button>
        <ToolButton onClick={() => view.zoomIn()}>＋</ToolButton>
      </aside>
      <section
        className="next-canvas"
        aria-label="React Konva 画布"
        onPointerDown={() => setContextMenu(null)}
      >
        <CanvasEditor />
        {maskBaseUrl &&
          maskSourceId &&
          store.doc.objects[maskSourceId] &&
          (() => {
            const source = store.doc.objects[maskSourceId]
            const point = view.worldToScreen({ x: source.x, y: source.y })
            return (
              <img
                className="next-mask-base"
                src={maskBaseUrl}
                alt=""
                style={{
                  left: point.x,
                  top: point.y,
                  width: source.width * snapshot.camera.scale,
                  height: source.height * snapshot.camera.scale,
                  transform: `rotate(${source.rotation ?? 0}deg)`,
                }}
              />
            )
          })()}
        {textEditor && (
          <textarea
            className="next-text-editor"
            aria-label="文字编辑器"
            autoFocus
            value={textEditor.value}
            style={{
              left: textEditor.rect.x,
              top: textEditor.rect.y,
              width: textEditor.rect.width,
              height: textEditor.rect.height,
            }}
            onChange={(event) =>
              setTextEditor((current) =>
                current ? { ...current, value: event.target.value } : current,
              )
            }
            onKeyDown={(event) => {
              event.stopPropagation()
              if (event.key === 'Escape') {
                setTextEditor(null)
              }
              if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
                commitTextEditor(view, store, textEditor)
                setTextEditor(null)
              }
            }}
            onBlur={() => {
              commitTextEditor(view, store, textEditor)
              setTextEditor(null)
            }}
          />
        )}
        {contextMenu && (
          <div
            className="next-context-menu"
            role="menu"
            style={{ left: contextMenu.point.x, top: contextMenu.point.y }}
            onPointerDown={(event) => event.stopPropagation()}
          >
            <button
              type="button"
              role="menuitem"
              onClick={() => view.focusObject(snapshot.selection[0] ?? '')}
            >
              查看详情
            </button>
            <button
              type="button"
              role="menuitem"
              disabled={!snapshot.capabilities.canEdit || snapshot.selection.length === 0}
              onClick={() => {
                view.duplicateSelection()
                setContextMenu(null)
              }}
            >
              复制
            </button>
            <button
              type="button"
              role="menuitem"
              disabled={!snapshot.capabilities.canDelete || snapshot.selection.length === 0}
              onClick={() => {
                view.deleteSelection()
                setContextMenu(null)
              }}
            >
              删除
            </button>
          </div>
        )}
        <form
          className="next-composer"
          onSubmit={(event) => {
            event.preventDefault()
            if (!prompt.trim() || generating) {
              return
            }
            setGenerating(true)
            void submitFromComposer(store, view, prompt.trim(), generationKind, generationDraft)
              .then((result) => {
                if (result.ok) {
                  setPrompt('')
                  setNotice('已提交生成任务')
                } else {
                  setNotice(result.error)
                }
              })
              .finally(() => setGenerating(false))
          }}
        >
          <textarea
            aria-label="生成提示词"
            value={prompt}
            placeholder="描述你想生成的画面…"
            onChange={(event) => setPrompt(event.target.value)}
            onKeyDown={(event) => {
              if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
                event.currentTarget.form?.requestSubmit()
              }
            }}
          />
          <div className="next-composer-options">
            <select
              aria-label="生成类型"
              value={generationKind}
              onChange={(event) => {
                const kind = event.target.value as MediaKind
                const next = defaultDraft(kind)
                next.model = capabilities?.[kind].models[0]?.id ?? ''
                setGenerationKind(kind)
                setGenerationDraft(next)
              }}
            >
              <option value="image">图片</option>
              <option value="video" disabled={!capabilities?.video.supported}>
                视频
              </option>
            </select>
            <select
              aria-label="生成模型"
              value={generationDraft.model}
              onChange={(event) =>
                setGenerationDraft((draft) => ({ ...draft, model: event.target.value }))
              }
            >
              {(capabilities?.[generationKind].models ?? []).map((model) => (
                <option key={model.id} value={model.id}>
                  {model.name}
                </option>
              ))}
            </select>
            <select
              aria-label="画面比例"
              value={generationDraft.ratio}
              onChange={(event) =>
                setGenerationDraft((draft) => ({ ...draft, ratio: event.target.value }))
              }
            >
              {(capabilities?.[generationKind].ratios ?? []).map((ratio) => (
                <option key={ratio}>{ratio}</option>
              ))}
            </select>
            <select
              aria-label="生成分辨率"
              value={generationDraft.resolution}
              onChange={(event) =>
                setGenerationDraft((draft) => ({
                  ...draft,
                  resolution: Number(event.target.value),
                }))
              }
            >
              {(capabilities?.[generationKind].resolutions ?? []).map((resolution) => (
                <option key={resolution} value={resolution}>
                  {resolution}px
                </option>
              ))}
            </select>
            {generationKind === 'video' && (
              <select
                aria-label="视频时长"
                value={generationDraft.durationSec}
                onChange={(event) =>
                  setGenerationDraft((draft) => ({
                    ...draft,
                    durationSec: Number(event.target.value),
                  }))
                }
              >
                {(capabilities?.video.durations ?? []).map((duration) => (
                  <option key={duration} value={duration}>
                    {duration}秒
                  </option>
                ))}
              </select>
            )}
          </div>
          <button type="submit" disabled={!prompt.trim() || generating || !generationDraft.model}>
            {generating ? '提交中…' : '生成'}
          </button>
        </form>
        {inpaintRequest && (
          <form
            className="next-operation-panel"
            onSubmit={(event) => {
              event.preventDefault()
              if (!inpaintRequest.prompt.trim() || generating) {
                return
              }
              setGenerating(true)
              void submitInpaint(
                store,
                view,
                inpaintRequest,
                capabilities?.image.models[0]?.id ?? '',
              )
                .then((result) => {
                  setNotice(result.ok ? '已提交局部重绘' : result.error)
                  if (result.ok) {
                    setInpaintRequest(null)
                    view.activateTool('select')
                  }
                })
                .catch((cause: unknown) => {
                  setNotice(cause instanceof Error ? cause.message : '局部重绘提交失败')
                })
                .finally(() => setGenerating(false))
            }}
          >
            <strong>局部重绘</strong>
            <input
              aria-label="局部重绘提示词"
              value={inpaintRequest.prompt}
              placeholder="描述区域内希望出现的内容"
              onChange={(event) =>
                setInpaintRequest((current) =>
                  current ? { ...current, prompt: event.target.value } : current,
                )
              }
            />
            <button type="submit" disabled={!inpaintRequest.prompt.trim() || generating}>
              提交
            </button>
            <button type="button" onClick={() => setInpaintRequest(null)}>
              取消
            </button>
          </form>
        )}
        {maskSourceId && (maskBaseUrl || snapshot.maskStrokes.length > 0) && (
          <div className="next-operation-panel">
            <strong>手动蒙版</strong>
            <span>
              {maskBaseUrl ? '已识别主体' : '手动蒙版'} · {snapshot.maskStrokes.length} 条修正笔画
            </span>
            <button
              type="button"
              disabled={generating}
              onClick={() => {
                setGenerating(true)
                void submitCutout(store, view, maskSourceId, snapshot.maskStrokes, maskBaseUrl)
                  .then(() => {
                    setNotice('抠图已完成')
                    view.clearMaskDraft()
                    setMaskSourceId(null)
                    setMaskBaseUrl(null)
                    view.activateTool('select')
                  })
                  .catch((cause: unknown) => {
                    setNotice(cause instanceof Error ? cause.message : '抠图失败')
                  })
                  .finally(() => setGenerating(false))
              }}
            >
              应用抠图
            </button>
            <button
              type="button"
              onClick={() => {
                view.clearMaskDraft()
                setMaskSourceId(null)
                setMaskBaseUrl(null)
                recognition.current?.cancel()
                recognition.current = null
                view.activateTool('select')
              }}
            >
              清除
            </button>
          </div>
        )}
      </section>
      <aside className="next-sidebar" aria-label="画布侧边栏">
        <nav className="next-sidebar-tabs" aria-label="侧边栏分类">
          {(
            [
              ['inspector', '属性'],
              ['library', '素材'],
              ['chat', '对话'],
              ['storyboard', '分镜'],
              ['timeline', '时间线'],
            ] as const
          ).map(([id, label]) => (
            <button
              key={id}
              type="button"
              className={sideTab === id ? 'is-active' : ''}
              onClick={() => setSideTab(id)}
            >
              {label}
            </button>
          ))}
        </nav>
        <SidebarContent
          tab={sideTab}
          store={store}
          view={view}
          selection={snapshot.selection}
          markDirty={() => saver.markDirty()}
        />
      </aside>
      <footer className="next-debug">
        Layer: {snapshot.effectiveLayer} · 选择 {snapshot.selection.length} 个对象
      </footer>
      {notice && (
        <div className="next-toast" role="status">
          {notice}
        </div>
      )}
    </main>
  )
}

function SidebarContent({
  tab,
  store,
  view,
  selection,
  markDirty,
}: {
  tab: 'inspector' | 'library' | 'chat' | 'storyboard' | 'timeline'
  store: DocStore
  view: ReturnType<typeof useCanvasView>
  selection: readonly string[]
  markDirty: () => void
}) {
  const object = selection.length === 1 ? view.getDocument().objects[selection[0]] : undefined
  const [message, setMessage] = useState('')
  if (tab === 'inspector') {
    if (!object) {
      return <p className="next-panel-empty">选择一个对象查看属性</p>
    }
    return (
      <div className="next-panel-body">
        <h2>{object.name || object.kind}</h2>
        <label>
          X
          <input
            type="number"
            value={Math.round(object.x)}
            onChange={(event) =>
              view.execute(
                cmdUpdateObject(object.id, { x: Number(event.target.value) }, { x: object.x }),
              )
            }
          />
        </label>
        <label>
          Y
          <input
            type="number"
            value={Math.round(object.y)}
            onChange={(event) =>
              view.execute(
                cmdUpdateObject(object.id, { y: Number(event.target.value) }, { y: object.y }),
              )
            }
          />
        </label>
        <label>
          宽度
          <input
            type="number"
            min="1"
            value={Math.round(object.width)}
            onChange={(event) =>
              view.execute(
                cmdUpdateObject(
                  object.id,
                  { width: Math.max(1, Number(event.target.value)) },
                  { width: object.width },
                ),
              )
            }
          />
        </label>
        <label>
          高度
          <input
            type="number"
            min="1"
            value={Math.round(object.height)}
            onChange={(event) =>
              view.execute(
                cmdUpdateObject(
                  object.id,
                  { height: Math.max(1, Number(event.target.value)) },
                  { height: object.height },
                ),
              )
            }
          />
        </label>
      </div>
    )
  }
  if (tab === 'library') {
    return <LibraryPanel view={view} />
  }
  if (tab === 'chat') {
    return (
      <div className="next-panel-body next-chat-panel">
        <div className="next-chat-list">
          {(store.doc.chat ?? []).map((item) => (
            <p key={item.id} className={`is-${item.role}`}>
              {item.text || item.prompt || item.request || item.kind}
            </p>
          ))}
        </div>
        <form
          onSubmit={(event) => {
            event.preventDefault()
            if (!message.trim()) {
              return
            }
            store.mutateTransient((document) => {
              document.chat ??= []
              document.chat.push({
                id: `chat-${crypto.randomUUID()}`,
                at: new Date().toISOString(),
                role: 'user',
                kind: 'text',
                text: message.trim(),
              })
            })
            markDirty()
            setMessage('')
          }}
        >
          <textarea value={message} onChange={(event) => setMessage(event.target.value)} />
          <button type="submit">发送</button>
        </form>
      </div>
    )
  }
  if (tab === 'storyboard') {
    const storyboard = store.doc.storyboard ?? emptyStoryboard()
    const applyStoryboard = (next: Storyboard) => {
      view.execute({
        type: 'setStoryboard',
        before: store.doc.storyboard ? structuredClone(store.doc.storyboard) : undefined,
        after: structuredClone(next),
      })
    }
    const updateShot = (id: string, patch: Partial<Storyboard['shots'][number]>) =>
      applyStoryboard({
        ...storyboard,
        shots: storyboard.shots.map((shot) => (shot.id === id ? { ...shot, ...patch } : shot)),
      })
    return (
      <div className="next-panel-body">
        <div className="next-row-actions">
          <h2>分镜 · {storyboard.shots.length}</h2>
          <button
            type="button"
            onClick={() =>
              applyStoryboard({ ...storyboard, shots: [...storyboard.shots, newShot()] })
            }
          >
            添加镜头
          </button>
        </div>
        <label className="next-wide-input">
          故事梗概
          <textarea
            value={storyboard.outline}
            onChange={(event) => applyStoryboard({ ...storyboard, outline: event.target.value })}
          />
        </label>
        {storyboard.shots.length === 0 ? (
          <p className="next-panel-empty">当前文档还没有分镜</p>
        ) : (
          storyboard.shots.map((shot, index) => (
            <section className="next-shot-card" key={shot.id}>
              <strong>镜头 {index + 1}</strong>
              <input
                aria-label={`镜头 ${index + 1} 标题`}
                value={shot.title}
                onChange={(event) => updateShot(shot.id, { title: event.target.value })}
              />
              <textarea
                aria-label={`镜头 ${index + 1} 画面`}
                placeholder="画面描述"
                value={shot.visual}
                onChange={(event) => updateShot(shot.id, { visual: event.target.value })}
              />
              <label>
                时长（帧）
                <input
                  type="number"
                  min="1"
                  value={shot.durationFrames}
                  onChange={(event) =>
                    updateShot(shot.id, {
                      durationFrames: Math.max(1, Math.round(Number(event.target.value) || 1)),
                    })
                  }
                />
              </label>
              <div className="next-row-actions">
                <button
                  type="button"
                  disabled={!object}
                  onClick={() => object && updateShot(shot.id, { nodeId: object.id })}
                >
                  {shot.nodeId ? '替换关联' : '关联所选节点'}
                </button>
                <button type="button" onClick={() => updateShot(shot.id, { locked: !shot.locked })}>
                  {shot.locked ? '解锁' : '锁定'}
                </button>
                <button
                  type="button"
                  onClick={() =>
                    applyStoryboard({
                      ...storyboard,
                      shots: storyboard.shots.filter((item) => item.id !== shot.id),
                    })
                  }
                >
                  删除
                </button>
              </div>
            </section>
          ))
        )}
      </div>
    )
  }
  const timeline = store.doc.timeline ?? emptyTimeline()
  const applyTimeline = (next: Timeline) => {
    view.execute({
      type: 'setTimeline',
      before: store.doc.timeline ? structuredClone(store.doc.timeline) : undefined,
      after: structuredClone(next),
    })
  }
  const addSelectedClip = () => {
    if (!object || (object.kind !== 'image' && object.kind !== 'video') || !object.src) {
      return
    }
    const durationFrames =
      object.kind === 'video'
        ? Math.max(1, Math.round((object.durationSec ?? 4) * timeline.fps))
        : 90
    applyTimeline({
      ...timeline,
      clips: [
        ...timeline.clips,
        {
          id: crypto.randomUUID(),
          name: object.name || object.kind,
          source: { nodeId: object.id, kind: object.kind, url: object.src, durationFrames },
          inFrame: 0,
          outFrame: durationFrames,
        },
      ],
    })
  }
  return (
    <div className="next-panel-body">
      <div className="next-row-actions">
        <h2>时间线 · {timeline.clips.length} 个片段</h2>
        <button type="button" disabled={!object} onClick={addSelectedClip}>
          添加所选素材
        </button>
      </div>
      {timeline.clips.map((clip, index) => (
        <section className="next-shot-card" key={clip.id}>
          <strong>{clip.name || `${clip.source.kind} ${index + 1}`}</strong>
          <label>
            入点
            <input
              type="number"
              min="0"
              max={clip.outFrame - 1}
              value={clip.inFrame}
              onChange={(event) => {
                const inFrame = Math.max(0, Math.min(clip.outFrame - 1, Number(event.target.value)))
                applyTimeline({
                  ...timeline,
                  clips: timeline.clips.map((item) =>
                    item.id === clip.id ? { ...item, inFrame } : item,
                  ),
                })
              }}
            />
          </label>
          <label>
            出点
            <input
              type="number"
              min={clip.inFrame + 1}
              max={clip.source.durationFrames}
              value={clip.outFrame}
              onChange={(event) => {
                const outFrame = Math.max(
                  clip.inFrame + 1,
                  Math.min(clip.source.durationFrames, Number(event.target.value)),
                )
                applyTimeline({
                  ...timeline,
                  clips: timeline.clips.map((item) =>
                    item.id === clip.id ? { ...item, outFrame } : item,
                  ),
                })
              }}
            />
          </label>
          <div className="next-row-actions">
            <button
              type="button"
              disabled={index === 0}
              onClick={() => applyTimeline(moveClip(timeline, clip.id, index - 1))}
            >
              前移
            </button>
            <button
              type="button"
              disabled={index === timeline.clips.length - 1}
              onClick={() => applyTimeline(moveClip(timeline, clip.id, index + 1))}
            >
              后移
            </button>
            <button
              type="button"
              onClick={() =>
                applyTimeline({
                  ...timeline,
                  clips: timeline.clips.filter((item) => item.id !== clip.id),
                })
              }
            >
              删除
            </button>
          </div>
        </section>
      ))}
      {timeline.clips.length === 0 && <p className="next-panel-empty">尚未装配时间线</p>}
    </div>
  )
}

type HistoryItem = {
  id: string
  url: string
  assetId?: string
  ext?: string
  params?: { kind?: string; width?: number; height?: number; prompt?: string }
}

function LibraryPanel({ view }: { view: ReturnType<typeof useCanvasView> }) {
  const [items, setItems] = useState<HistoryItem[]>([])
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(true)
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    void fetch(`/api/history?limit=100${query ? `&q=${encodeURIComponent(query)}` : ''}`, {
      signal: controller.signal,
    })
      .then((response) => response.json())
      .then((body: { images?: HistoryItem[] }) => {
        if (!controller.signal.aborted) {
          setItems(body.images ?? [])
          setLoading(false)
        }
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setLoading(false)
        }
      })
    return () => controller.abort()
  }, [query])

  const add = (item: HistoryItem) => {
    const width = Math.min(420, item.params?.width || 320)
    const sourceHeight = item.params?.height || 320
    const sourceWidth = item.params?.width || 320
    const height = Math.round((width / sourceWidth) * sourceHeight)
    const center = view.screenToWorld({
      x: view.getSnapshot().viewport.width / 2,
      y: view.getSnapshot().viewport.height / 2,
    })
    view.execute(
      cmdAddObjects([
        {
          id: `asset-${crypto.randomUUID()}`,
          kind: item.params?.kind === 'video' ? 'video' : 'image',
          x: center.x - width / 2,
          y: center.y - height / 2,
          width,
          height,
          src: item.url,
          imageId: item.id,
          assetId: item.assetId,
          ext: item.ext,
          name: item.params?.prompt,
        },
      ]),
    )
  }

  return (
    <div className="next-panel-body">
      <input
        aria-label="搜索素材"
        value={query}
        placeholder="搜索素材"
        onChange={(event) => setQuery(event.target.value)}
      />
      <label className="next-upload-button">
        上传素材
        <input
          type="file"
          accept="image/*,video/*"
          onChange={(event) => {
            const file = event.target.files?.[0]
            if (!file) {
              return
            }
            const kind = file.type.startsWith('video/') ? 'video' : 'image'
            const ext = file.name.split('.').pop() || (kind === 'video' ? 'mp4' : 'png')
            void fetch(
              `/api/assets?kind=${kind}&ext=${encodeURIComponent(ext)}&name=${encodeURIComponent(file.name)}`,
              { method: 'POST', headers: { 'Content-Type': file.type }, body: file },
            )
              .then((response) => response.json())
              .then(
                (body: { asset?: { id: string; ext: string }; urls?: { original?: string } }) => {
                  if (body.asset && body.urls?.original) {
                    add({
                      id: body.asset.id,
                      assetId: body.asset.id,
                      ext: body.asset.ext,
                      url: body.urls.original,
                      params: { kind },
                    })
                  }
                },
              )
          }}
        />
      </label>
      <div className="next-library-grid">
        {items.map((item) => (
          <button key={item.id} type="button" onClick={() => add(item)} title="加入画布">
            {item.params?.kind === 'video' ? (
              <video src={item.url} muted preload="metadata" />
            ) : (
              <img src={item.url} alt={item.params?.prompt || '素材'} loading="lazy" />
            )}
          </button>
        ))}
      </div>
      {!loading && items.length === 0 && <p className="next-panel-empty">暂无素材</p>}
    </div>
  )
}

async function submitFromComposer(
  store: DocStore,
  view: ReturnType<typeof useCanvasView>,
  prompt: string,
  kind: MediaKind,
  draft: ReturnType<typeof defaultDraft>,
) {
  const selected = view.getSnapshot().selection[0]
  const source = selected ? view.getDocument().objects[selected] : undefined
  const initImage =
    source && (source.kind === 'image' || source.kind === 'video')
      ? { dataUrl: await objectToDataUrl(source) }
      : undefined
  const anchor = source
    ? { x: source.x + source.width + 40, y: source.y }
    : view.screenToWorld({ x: 180, y: 180 })
  const size = outputSize(draft)
  const genParams = {
    prompt,
    negativePrompt: '',
    model: draft.model,
    width: size.width,
    height: size.height,
    steps: 20,
    cfgScale: 7,
    seed: -1,
    sampler: 'euler',
    scheduler: 'normal',
    denoise: initImage ? draft.denoise : 1,
    kind,
    durationSec: draft.durationSec,
    fps: 16,
  }
  return submitGeneration({
    docStore: store,
    genParams,
    anchor,
    initImage,
    lineage: source ? { fromId: source.id, params: genParams } : undefined,
  })
}

async function submitInpaint(
  store: DocStore,
  view: ReturnType<typeof useCanvasView>,
  request: InpaintRequest,
  model: string,
) {
  const source = view.getDocument().objects[request.sourceId]
  if (!source || source.kind !== 'image') {
    throw new Error('局部重绘源图已不存在')
  }
  const initDataUrl = await objectToDataUrl(source)
  const image = await loadImage(initDataUrl)
  const canvas = document.createElement('canvas')
  canvas.width = image.naturalWidth
  canvas.height = image.naturalHeight
  const context = canvas.getContext('2d')
  if (!context) {
    throw new Error('无法创建蒙版')
  }
  context.fillStyle = '#000'
  context.fillRect(0, 0, canvas.width, canvas.height)
  context.fillStyle = '#fff'
  context.fillRect(
    ((request.region.x - source.x) / source.width) * canvas.width,
    ((request.region.y - source.y) / source.height) * canvas.height,
    (request.region.width / source.width) * canvas.width,
    (request.region.height / source.height) * canvas.height,
  )
  const genParams = {
    prompt: request.prompt.trim(),
    negativePrompt: '',
    model,
    width: canvas.width,
    height: canvas.height,
    steps: 20,
    cfgScale: 7,
    seed: -1,
    sampler: 'euler',
    scheduler: 'normal',
    denoise: 1,
    kind: 'image',
    durationSec: 4,
    fps: 16,
    inpaint: true,
  }
  return submitGeneration({
    docStore: store,
    genParams,
    anchor: { x: source.x + source.width + 40, y: source.y },
    initImage: { dataUrl: initDataUrl },
    maskImage: { dataUrl: canvas.toDataURL('image/png') },
    lineage: { fromId: source.id, params: genParams },
  })
}

function loadImage(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image()
    image.onload = () => resolve(image)
    image.onerror = () => reject(new Error('源图读取失败'))
    image.src = url
  })
}

async function submitCutout(
  store: DocStore,
  view: ReturnType<typeof useCanvasView>,
  sourceId: string,
  strokes: readonly (readonly Point[])[],
  baseMaskUrl: string | null,
): Promise<void> {
  const source = view.getDocument().objects[sourceId]
  if (!source || source.kind !== 'image') {
    throw new Error('抠图源图已不存在')
  }
  const asset = await ensureAsset(source)
  const image = await loadImage(await objectToDataUrl(source))
  const canvas = document.createElement('canvas')
  canvas.width = image.naturalWidth
  canvas.height = image.naturalHeight
  const context = canvas.getContext('2d')
  if (!context) {
    throw new Error('无法创建蒙版')
  }
  if (baseMaskUrl) {
    const baseMask = await loadImage(baseMaskUrl)
    context.drawImage(baseMask, 0, 0, canvas.width, canvas.height)
  } else {
    context.fillStyle = '#000'
    context.fillRect(0, 0, canvas.width, canvas.height)
  }
  context.strokeStyle = '#fff'
  context.lineCap = 'round'
  context.lineJoin = 'round'
  context.lineWidth = (22 / source.width) * canvas.width
  for (const stroke of strokes) {
    if (stroke.length === 0) {
      continue
    }
    context.beginPath()
    context.moveTo(
      ((stroke[0].x - source.x) / source.width) * canvas.width,
      ((stroke[0].y - source.y) / source.height) * canvas.height,
    )
    for (const point of stroke.slice(1)) {
      context.lineTo(
        ((point.x - source.x) / source.width) * canvas.width,
        ((point.y - source.y) / source.height) * canvas.height,
      )
    }
    context.stroke()
  }
  const mask = await (await fetch(canvas.toDataURL('image/png'))).blob()
  const response = await fetch(`/api/cutout/apply/${encodeURIComponent(asset.id)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'image/png' },
    body: mask,
  })
  const body = (await response.json()) as {
    error?: string
    asset?: { id: string; ext: string; hasThumbs?: boolean }
    mask?: { id: string }
  }
  if (!response.ok || !body.asset || !body.mask) {
    throw new Error(body.error || '抠图失败')
  }
  store.apply(
    cmdAddObjects([
      {
        id: `cutout-${crypto.randomUUID()}`,
        kind: 'image',
        x: source.x + source.width + 24,
        y: source.y,
        width: source.width,
        height: source.height,
        assetId: body.asset.id,
        ext: body.asset.ext,
        hasThumbs: body.asset.hasThumbs,
        lineage: { fromId: source.id, params: { operation: 'cutout' } },
        cutout: {
          originalAssetId: asset.id,
          originalExt: asset.ext,
          maskAssetId: body.mask.id,
        },
      },
    ]),
  )
}

function commitTextEditor(
  view: ReturnType<typeof useCanvasView>,
  store: DocStore,
  editor: TextEditorState,
): void {
  const object = store.doc.objects[editor.objectId]
  if (!object || object.kind !== 'text' || object.text === editor.value) {
    return
  }
  view.execute(cmdUpdateObject(object.id, { text: editor.value }, { text: object.text }))
}

function ToolButton({
  active = false,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { active?: boolean }) {
  return <button className={active ? 'is-active' : ''} type="button" {...props} />
}

function saveLabel(status: SaveStatus): string {
  if (status === 'saving') {
    return '保存中…'
  }
  if (status === 'saved') {
    return '已保存'
  }
  if (status === 'conflict') {
    return '保存冲突'
  }
  if (status === 'error') {
    return '保存失败'
  }
  return 'React-Konva 预览'
}

const root = document.getElementById('canvas-next-root')
if (!root) {
  throw new Error('缺少 canvas-next-root')
}
createRoot(root).render(
  <StrictMode>
    <CanvasNextApp />
  </StrictMode>,
)
