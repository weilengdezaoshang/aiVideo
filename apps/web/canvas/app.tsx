import { installSessionFetch } from '../shared/session-fetch.js'
installSessionFetch()

import { StrictMode, useEffect, useMemo, useRef, useState, type ButtonHTMLAttributes } from 'react'
import { createRoot } from 'react-dom/client'

import { applyTheme, loadTheme, saveTheme, type Theme } from './theme.js'

applyTheme(loadTheme() ?? 'light')

import { selectionSurface, mediaSurface, selectionAnchor } from './selection-surface.js'
import { downloadUrl } from './flows/asset-util.js'
import { mediaSource } from './state/studio-bindings.js'
import { ComposerPresence, SidebarPresence } from './panel-presence.js'

import { ContentToolbar, ViewportControls, insertCanvasPlaceholder } from './studio-controls.js'

import { CanvasEditor } from './renderer-konva/index.js'
import {
  CanvasViewProvider,
  useCanvasView,
  useCanvasViewSnapshot,
} from './react/canvas-view-context.js'
import { createAutoSaver, type SaveStatus } from './state/autosave.js'
import { cmdAddObjects, cmdMoveObjects, cmdUpdateObject, type CanvasObj } from './state/commands.js'
import { connectReference } from './flows/connect-reference.js'
import { getGenerationCoordinator, submitGeneration } from './flows/generate.js'
import { ensureAsset, objectToDataUrl } from './flows/asset-util.js'
import {
  addFavorite,
  isFavorite,
  loadFavorites,
  recentPrompts,
  removeFavorite,
  type PromptFavorite,
} from './flows/prompt-library.js'
import {
  DRAFT_STEPS,
  draftSize,
  fullQualityParams,
  isDraftParams,
  lockedSeed,
  seedOf,
} from './flows/draft-mode.js'
import { connectGenerationEvents } from './state/generation-events.js'
import {
  createCanvasRecognition,
  type RecognitionOperation,
} from './flows/recognition-operation.js'
import type { CanvasHitTarget, Point, Rect } from './core/types.js'
import { CanvasDocumentGate } from './document-gate.js'
import { AgentPanel } from './agent-panel.js'
import { createAgentStore, type AgentStore } from './agent/store.js'
import { createDocStore, type CanvasDocData, type DocStore } from './state/doc-store.js'
import {
  defaultDraft,
  migrateNodes,
  outputSize,
  type Capabilities,
  type MediaKind,
} from './state/node-model.js'
import { emptyStoryboard, newShot, type Storyboard } from './state/storyboard.js'
import { TimelineStudio } from './timeline-studio.js'
import { SvgStudio } from './svg-studio.js'
import { saveNodeDraft, timelineNodeStore, detachReference } from './state/studio-bindings.js'
import { documentConnections } from './state/connections.js'
import {
  submitGroupGeneration,
  confirmPendingGroup,
  cancelPendingGroup,
} from './flows/group-generate.js'
type LoadedCanvas = { document: CanvasDocData; store: DocStore; restored?: boolean }
type TextEditorState = { objectId: string; rect: Rect; value: string }
type ContextMenuState = { target: CanvasHitTarget | null; point: Point }
type InpaintRequest = { sourceId: string; region: Rect; prompt: string }

function CanvasApp() {
  return (
    <CanvasDocumentGate>
      {(document, restored) => <ReadyCanvas document={document} restored={restored} />}
    </CanvasDocumentGate>
  )
}

function ReadyCanvas({ document, restored }: { document: CanvasDocData; restored: boolean }) {
  const [store] = useState(() => {
    const copy = structuredClone(document)
    migrateNodes(copy.objects)
    return createDocStore(copy)
  })
  return (
    <CanvasViewProvider store={store}>
      <CanvasShell document={document} store={store} restored={restored} />
    </CanvasViewProvider>
  )
}

function CanvasShell({ document, store, restored }: LoadedCanvas) {
  const view = useCanvasView()
  const snapshot = useCanvasViewSnapshot()
  const [saveStatus, setSaveStatus] = useState<SaveStatus>('idle')
  const [notice, setNotice] = useState('')
  const [theme, setTheme] = useState<Theme>(() => loadTheme() ?? 'light')
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [documentName, setDocumentName] = useState(document.name)
  const [lookupOpen, setLookupOpen] = useState(false)
  const [lookupQuery, setLookupQuery] = useState('')
  const renameCancelled = useRef(false)
  const chatTrigger = useRef<HTMLButtonElement>(null)
  const sidebarWasOpen = useRef(false)
  const panelCloseButton = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    const canvasHasFocus = window.document.activeElement?.closest('[data-canvas-keyboard-target]')
    if (sidebarOpen && !canvasHasFocus) {
      panelCloseButton.current?.focus()
    } else if (!sidebarOpen && sidebarWasOpen.current && !canvasHasFocus) {
      chatTrigger.current?.focus()
    }
    sidebarWasOpen.current = sidebarOpen
  }, [sidebarOpen])
  const [textEditor, setTextEditor] = useState<TextEditorState | null>(null)
  const [contextMenu, setContextMenu] = useState<ContextMenuState | null>(null)
  const [prompt, setPrompt] = useState('')
  const draftDirty = useRef(false)
  const [composerNodeId, setComposerNodeId] = useState<string | null>(null)
  const [timelineNodeId, setTimelineNodeId] = useState<string | undefined>()
  const [svgObject, setSvgObject] = useState<CanvasObj | null>(null)
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null)
  const [generationKind, setGenerationKind] = useState<MediaKind>('image')
  const [generationDraft, setGenerationDraft] = useState(() => defaultDraft('image'))
  const [generating, setGenerating] = useState(false)
  const [promptLibraryOpen, setPromptLibraryOpen] = useState(false)
  const [promptQuery, setPromptQuery] = useState('')
  const [favorites, setFavorites] = useState(() => loadFavorites(localStorage))
  const [draftMode, setDraftMode] = useState(false)
  const [seedLocked, setSeedLocked] = useState(false)
  const [seedValue, setSeedValue] = useState('')
  const [connected, setConnected] = useState(true)
  const [inpaintRequest, setInpaintRequest] = useState<InpaintRequest | null>(null)
  const [maskSourceId, setMaskSourceId] = useState<string | null>(null)
  const [maskBaseUrl, setMaskBaseUrl] = useState<string | null>(null)
  const recognition = useRef<RecognitionOperation | null>(null)
  const [sideTab, setSideTab] = useState<
    'inspector' | 'library' | 'chat' | 'storyboard' | 'timeline'
  >('inspector')
  const selectedObjectId = snapshot.selection.length === 1 ? snapshot.selection[0] : null
  const selectedObjects = snapshot.selection.flatMap((id) => {
    const object = view.getDocument().objects[id]
    return object ? [{ ...object, ...snapshot.objectPreviews[id] }] : []
  })
  const surface = selectionSurface(selectedObjects)
  const selectedObject = selectedObjects.length === 1 ? selectedObjects[0] : undefined
  const anchor = selectionAnchor(selectedObjects, snapshot.camera, snapshot.viewport)
  const [previewObject, setPreviewObject] = useState<CanvasObj | null>(null)
  const inlineTimelineStore = useMemo(
    () =>
      timelineNodeStore(
        store,
        surface === 'timeline' ? (selectedObjectId ?? undefined) : undefined,
      ),
    [store, surface, selectedObjectId],
  )
  function configureMedia(object: CanvasObj) {
    draftDirty.current = false
    const kind = object.mediaType ?? (object.kind === 'video' ? 'video' : 'image')
    const params = ((object.generationId
      ? store.doc.generations?.[object.generationId]?.spec.params
      : undefined) ??
      object.generationSpec?.params ??
      object.lineage?.params ??
      {}) as Record<string, unknown>
    const defaults = defaultDraft(kind)
    const draft = object.nodeDraft ?? {
      ...defaults,
      prompt: typeof params.prompt === 'string' ? params.prompt : defaults.prompt,
      model: typeof params.model === 'string' ? params.model : defaults.model,
      durationSec:
        typeof params.durationSec === 'number' ? params.durationSec : defaults.durationSec,
    }
    setComposerNodeId(object.id)
    setGenerationKind(kind)
    setGenerationDraft({ ...draft, model: draft.model || capabilities?.[kind].models[0]?.id || '' })
    setPrompt(draft.prompt)
    setSidebarOpen(false)
  }
  const composerRef = useRef<HTMLFormElement>(null)
  const [composerHeight, setComposerHeight] = useState(220)
  const [composerWidth, setComposerWidth] = useState(620)
  useEffect(() => {
    const element = composerRef.current
    if (!element) {
      return
    }
    const observer = new ResizeObserver(() => {
      const bounds = element.getBoundingClientRect()
      setComposerHeight(bounds.height)
      setComposerWidth(bounds.width)
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [surface])
  const toolbarRef = useRef<HTMLDivElement>(null)
  const [toolbarWidth, setToolbarWidth] = useState(0)
  const [toolbarHeight, setToolbarHeight] = useState(44)
  useEffect(() => {
    const element = toolbarRef.current
    if (!element) {
      return
    }
    const observer = new ResizeObserver(() => {
      const rect = element.getBoundingClientRect()
      setToolbarWidth(rect.width)
      setToolbarHeight(rect.height)
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [surface, selectedObjectId, snapshot.capabilities.canUndo, snapshot.capabilities.canRedo])
  const previousSelection = useRef<string | null>(null)
  useEffect(() => {
    const selectionKey = `${selectedObjectId ?? ''}:${surface}`
    if (selectionKey === previousSelection.current) {
      return
    }
    previousSelection.current = selectionKey
    setComposerNodeId(null)
    if (!selectedObjectId) {
      setSidebarOpen(false)
      return
    }
    const object = store.doc.objects[selectedObjectId]
    if (!object) {
      return
    }
    setSidebarOpen(false)
    if (mediaSurface(selectionSurface([object]))) {
      configureMedia(object)
    }
    if (object.placeholderType === 'timeline') {
      setTimelineNodeId(object.id)
    }
  }, [selectedObjectId, surface, store, capabilities])
  useEffect(
    () =>
      view.on('inspector:requested', () => {
        setSideTab('inspector')
        setSidebarOpen(true)
      }),
    [view],
  )
  useEffect(() => {
    if (draftDirty.current && composerNodeId && composerNodeId === selectedObjectId) {
      draftDirty.current = false
      saveNodeDraft(store, composerNodeId, { ...generationDraft, prompt })
    }
  }, [composerNodeId, selectedObjectId, generationDraft, prompt, store])
  /** 视野适配分两阶段：空画布居中一次；内容首次就绪再框一次，避免“文档加载晚于视口就绪”时内容落在视野外。 */
  const fittedStage = useRef<'empty' | 'content' | null>(null)
  const objectCount = view.getDocument().order.length
  useEffect(() => {
    if (snapshot.viewport.width <= 1 || snapshot.viewport.height <= 1) {
      return
    }
    const stage = objectCount > 0 ? 'content' : 'empty'
    if (fittedStage.current === 'content' || fittedStage.current === stage) {
      return
    }
    fittedStage.current = stage
    view.fitToContent()
  }, [snapshot.viewport.height, snapshot.viewport.width, view, objectCount])
  const saver = useMemo(
    () =>
      createAutoSaver({
        docId: document.id,
        getDoc: () => store.doc,
        onStatus: setSaveStatus,
      }),
    [document.id, store],
  )
  const generationCoordinator = useMemo(
    () => getGenerationCoordinator(store, { ensureSaved: () => saver.ensureSaved() }),
    [store, saver],
  )
  const agentStore = useMemo(
    () => createAgentStore({ documentId: document.id, ensureSaved: () => saver.ensureSaved() }),
    [document.id, saver],
  )
  useEffect(() => agentStore.start(), [agentStore])
  useEffect(() => {
    if (restored) {
      saver.markDirty()
    }
  }, [restored, saver])

  useEffect(() => {
    return () => recognition.current?.cancel()
  }, [])
  useEffect(() => {
    const sourceId = maskSourceId
    if (
      sourceId &&
      (snapshot.selection.length !== 1 ||
        snapshot.selection[0] !== sourceId ||
        !['recognizing', 'mask'].includes(snapshot.requestedTool))
    ) {
      recognition.current?.cancel()
      recognition.current = null
      setMaskSourceId(null)
      setMaskBaseUrl(null)
      view.clearMaskDraft()
      if (['recognizing', 'mask'].includes(snapshot.requestedTool)) {
        view.activateTool('select')
      }
    }
  }, [maskSourceId, snapshot.selection, snapshot.requestedTool, view])

  useEffect(() => {
    const unsubscribe = store.subscribe((event) => event.type !== 'transient' && saver.markDirty())
    return () => {
      unsubscribe()
    }
  }, [saver, store])
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
      onGroupPublished: () => saver.markDirty(),
    })
    return () => events.close()
  }, [generationCoordinator, store, saver])
  useEffect(() => {
    const unsubscribe = generationCoordinator.runtime.subscribe(() => {
      store.mutateTransient(() => undefined)
    })
    return () => {
      unsubscribe()
    }
  }, [generationCoordinator, store, saver])
  useEffect(() => {
    const controller = new AbortController()
    void fetch('/api/generation-capabilities', { signal: controller.signal })
      .then((response) => response.json())
      .then((body: Capabilities) => {
        if (!controller.signal.aborted) {
          setCapabilities(body)
          const id = view.getSnapshot().selection[0]
          const object = id ? store.doc.objects[id] : undefined
          const kind = object?.mediaType ?? (object?.kind === 'video' ? 'video' : 'image')
          setGenerationDraft((draft) => ({
            ...draft,
            model: draft.model || body[kind].models[0]?.id || '',
          }))
        }
      })
      .catch(() => {})
    return () => controller.abort()
  }, [store, view])
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
      view.on('asset-picker:requested', () => {
        setSideTab('library')
        setSidebarOpen(true)
      }),
    [view],
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
  return (
    <main className={`editor-body ${sidebarOpen ? 'chat-open-narrow' : 'chat-collapsed'}`}>
      {svgObject && <SvgStudio object={svgObject} onClose={() => setSvgObject(null)} />}
      <header className="topbar">
        <a
          className="back-link identity-brand identity-editor-brand"
          href="/workspace?tab=canvas"
          aria-label="返回我的画布"
          title="帧屿集 FRAYUNE · 返回我的画布"
        >
          <span className="identity-mark" aria-hidden="true" />
          <span className="identity-wordmark" aria-hidden="true">
            <span className="identity-en" />
            <span className="identity-cn" />
          </span>
        </a>
        <div className="canvas-title-group">
          <input
            className="doc-name"
            aria-label="画布名称"
            value={documentName}
            maxLength={60}
            autoComplete="off"
            spellCheck={false}
            onFocus={() => {
              renameCancelled.current = false
            }}
            onChange={(event) => setDocumentName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                renameCancelled.current = true
                event.currentTarget.blur()
              }
              if (event.key === 'Enter') {
                event.currentTarget.blur()
              }
            }}
            onBlur={() => {
              const name = renameCancelled.current
                ? store.doc.name
                : documentName.trim() || '未命名画布'
              setDocumentName(name)
              if (name !== store.doc.name) {
                store.doc.name = name
                saver.markDirty()
              }
            }}
          />
          <span className="topbar-status" id="save-status">
            {saveLabel(saveStatus)}
          </span>
        </div>
        <span className="topbar-spacer" />
        <div className="topbar-tools">
          <button
            className="icon-btn"
            type="button"
            aria-label="搜索画布"
            aria-expanded={lookupOpen}
            onClick={() => setLookupOpen(!lookupOpen)}
          >
            <svg
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.7"
              aria-hidden="true"
            >
              <circle cx="10.5" cy="10.5" r="6.5" />
              <path d="m16 16 5 5" />
            </svg>
          </button>
          <button
            className="icon-btn"
            type="button"
            aria-label="历史对话"
            onClick={() => {
              setSideTab('chat')
              setSidebarOpen(true)
            }}
          >
            <svg
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.7"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <path d="M3 11a9 9 0 112 7M3 5v6h6m3-4v5l3 2" />
            </svg>
          </button>
          <button
            className="icon-btn"
            type="button"
            aria-label="画布属性"
            onClick={() => {
              setSideTab('inspector')
              setSidebarOpen(true)
            }}
          >
            ⓘ
          </button>
          <details className="user-menu">
            <summary className="icon-btn user-avatar-trigger" aria-label="用户菜单">
              <span className="user-avatar" aria-hidden="true">
                <svg
                  width="18"
                  height="18"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.7"
                >
                  <circle cx="12" cy="8" r="3.25" />
                  <path d="M5.5 20a6.5 6.5 0 0113 0" />
                </svg>
              </span>
            </summary>
            <div className="user-menu-popover" role="menu" aria-label="用户菜单">
              <div className="user-menu-profile">
                <strong>帧屿集创作者</strong>
              </div>
              <button
                type="button"
                role="menuitem"
                aria-label={theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'}
                onClick={() => {
                  const next: Theme = theme === 'dark' ? 'light' : 'dark'
                  applyTheme(next)
                  saveTheme(next)
                  setTheme(next)
                }}
              >
                <span aria-hidden="true">{theme === 'dark' ? '☼' : '☾'}</span>
                {theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'}
              </button>
              <div className="user-menu-status">{connected ? '已连接' : '正在重连'}</div>
              <a href="/workspace?tab=canvas">返回我的画布</a>
            </div>
          </details>
        </div>
        {saveStatus === 'conflict' && (
          <button onClick={() => window.location.reload()}>重新读取并选择保留版本</button>
        )}
        {saveStatus === 'error' && <button onClick={() => void saver.flush()}>重试保存</button>}
      </header>
      {lookupOpen && (
        <section className="canvas-lookup studio-lookup" aria-label="搜索画布对象">
          <input
            aria-label="搜索画布对象"
            placeholder="搜索画布中的对象…"
            value={lookupQuery}
            onChange={(event) => setLookupQuery(event.target.value)}
          />
          {view
            .getDocument()
            .order.filter((id) => {
              const object = view.getDocument().objects[id]
              return (
                object &&
                `${object.name || ''} ${object.text || ''} ${object.kind}`
                  .toLowerCase()
                  .includes(lookupQuery.toLowerCase())
              )
            })
            .map((id) => (
              <button
                type="button"
                key={id}
                onClick={() => {
                  view.select([id])
                  view.centerOnObject(id)
                  setLookupOpen(false)
                }}
              >
                {view.getDocument().objects[id].name ||
                  view.getDocument().objects[id].text ||
                  view.getDocument().objects[id].kind}
              </button>
            ))}
          {view.getDocument().order.length === 0 && <p>画布中还没有对象</p>}
        </section>
      )}
      {Object.entries(store.doc.groups ?? {})
        .filter(([, group]) => group.slots.some((id) => store.doc.objects[id]?.pendingConfirm))
        .map(([id]) => (
          <section key={id} role="status">
            组生成结果待确认
            <button
              type="button"
              onClick={() => {
                void confirmPendingGroup({ docStore: store, groupId: id }).then((result) => {
                  if (result.found) {
                    saver.markDirty()
                  }
                  setNotice(
                    result.found ? '已找到原组请求，正在恢复结果' : '尚未确认结果，请稍后再次查询',
                  )
                })
              }}
            >
              查询原组结果
            </button>
            <button
              type="button"
              onClick={() => {
                void cancelPendingGroup({ docStore: store, groupId: id })
                  .then(() => {
                    saver.markDirty()
                    setNotice('原组请求已取消')
                  })
                  .catch((error: unknown) =>
                    setNotice(error instanceof Error ? error.message : '取消失败'),
                  )
              }}
            >
              取消原组请求
            </button>
          </section>
        ))}
      {generationCoordinator.runtime
        .values()
        .filter(
          (entry) => !entry.jobId && ['uncertain', 'queued', 'disconnected'].includes(entry.status),
        )
        .map((entry) => (
          <section className="next-request-recovery" role="status" key={entry.operationId}>
            <span>有一个生成请求的提交结果待确认</span>
            <button
              onClick={() =>
                void generationCoordinator
                  .recover(entry.generationId)
                  .then((result) =>
                    setNotice(result.ok ? '已找回原任务' : result.error || '结果待确认'),
                  )
              }
            >
              查询原请求
            </button>
            <button
              onClick={() =>
                void generationCoordinator
                  .cancel(entry.generationId)
                  .then(() => setNotice('原请求已取消，可以重新生成'))
                  .catch((cause: unknown) =>
                    setNotice(cause instanceof Error ? cause.message : '取消未确认'),
                  )
              }
            >
              取消原请求
            </button>
          </section>
        ))}
      <ContentToolbar
        onImage={() => insertCanvasPlaceholder(view, 'image')}
        onVideo={() => insertCanvasPlaceholder(view, 'video')}
        onTimeline={() => insertCanvasPlaceholder(view, 'timeline')}
        onLibrary={() => {
          setSideTab('library')
          setSidebarOpen(true)
        }}
        onBoard={() => {
          const center = view.screenToWorld({
            x: snapshot.viewport.width / 2,
            y: snapshot.viewport.height / 2,
          })
          const id = `board-${crypto.randomUUID()}`
          view.execute(
            cmdAddObjects([
              {
                id,
                kind: 'board',
                name: '画板',
                x: center.x - 320,
                y: center.y - 180,
                width: 640,
                height: 360,
                backgroundColor: '#ffffff',
              },
            ]),
          )
          view.select([id])
          view.activateTool('select')
        }}
        onText={() => view.activateTool('text')}
      />

      <section
        className="canvas-root"
        aria-label="React Konva 画布"
        onPointerDown={() => setContextMenu(null)}
      >
        <CanvasEditor theme={theme} />
        <ViewportControls view={view} tool={snapshot.requestedTool} scale={snapshot.camera.scale} />
        {['image', 'video', 'text', 'multi', 'board', 'busy', 'error'].includes(surface) &&
          snapshot.selection.length > 0 && (
            <div
              key={`${selectedObjectId ?? 'multi'}:${surface}`}
              className="canvas-selection-tools"
              aria-label="选中对象操作"
              role="toolbar"
              ref={toolbarRef}
              style={
                snapshot.selection.length
                  ? {
                      left: Math.max(
                        toolbarWidth / 2 + 76,
                        Math.min(snapshot.viewport.width - toolbarWidth / 2 - 16, anchor.x),
                      ),
                      top: anchor.top,
                    }
                  : undefined
              }
            >
              {surface === 'multi' && (
                <span className="selection-count">{selectedObjects.length} 节点</span>
              )}
              {surface === 'image' && (
                <>
                  <ToolButton onClick={() => selectedObject && configureMedia(selectedObject)}>
                    智能改图
                  </ToolButton>
                  <ToolButton disabled title="当前生成后端尚未支持720全景">
                    720 全景
                  </ToolButton>
                  <ToolButton disabled title="当前生成后端尚未支持扩图">
                    扩图
                  </ToolButton>
                  <ToolButton disabled title="当前生成后端尚未支持超清">
                    智能超清
                  </ToolButton>
                </>
              )}
              {surface === 'video' && (
                <>
                  {['局部重拍', '智能超清', '视频编辑', '截取帧', '视频修剪'].map((label) => (
                    <ToolButton key={label} disabled title="该视频处理能力尚未接入">
                      {label}
                    </ToolButton>
                  ))}
                </>
              )}
              {(surface === 'image' || surface === 'board') && (
                <>
                  <ToolButton
                    active={snapshot.requestedTool === 'connect'}
                    onClick={() => view.activateTool('connect')}
                  >
                    连线
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
                      const operation = createCanvasRecognition(
                        view,
                        source.id,
                        () =>
                          store.doc.objects[source.id] === rawSource &&
                          recognition.current === operation,
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
                            operation.cancel()
                            if (view.getSnapshot().requestedTool === 'recognizing') {
                              view.activateTool('select')
                            }
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
                </>
              )}
              {surface === 'image' && (
                <>
                  {['多角度', '智能打光'].map((label) => (
                    <ToolButton key={label} disabled title="该图片处理能力尚未接入">
                      {label}
                    </ToolButton>
                  ))}
                </>
              )}
              {surface === 'image' && (
                <details className="selection-menu">
                  <summary>预设</summary>
                  <div>
                    {[
                      '场景俯视图',
                      '连续分镜图',
                      '多机位九宫格',
                      '人物三视图',
                      '面部三视图',
                      '产品三视图',
                    ].map((label) => (
                      <button type="button" disabled key={label} title="该预设生成能力尚未接入">
                        {label}
                      </button>
                    ))}
                  </div>
                </details>
              )}
              {surface === 'image' && (
                <details className="selection-menu">
                  <summary>工具</summary>
                  <div>
                    {['消除笔', '裁剪', '宫格切分', '标注'].map((label) => (
                      <button type="button" disabled key={label} title="该编辑能力尚未接入">
                        {label}
                      </button>
                    ))}
                    <button type="button" onClick={() => view.activateTool('connect')}>
                      连线
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        if (selectedObject) {
                          setSvgObject(selectedObject)
                        }
                      }}
                    >
                      导出 SVG
                    </button>
                  </div>
                </details>
              )}
              {surface === 'multi' && (
                <>
                  <ToolButton disabled title="批量超清尚未接入">
                    智能超清
                  </ToolButton>
                  <ToolButton disabled title="批量抠图尚未接入">
                    抠图
                  </ToolButton>
                  <ToolButton disabled title="用户编组尚未接入">
                    编组
                  </ToolButton>
                  <details className="selection-menu">
                    <summary>布局</summary>
                    <div>
                      <button
                        type="button"
                        disabled={!snapshot.capabilities.canEdit}
                        onClick={() => {
                          const columns = Math.ceil(Math.sqrt(selectedObjects.length))
                          const width =
                            Math.max(...selectedObjects.map((object) => object.width)) + 40
                          const height =
                            Math.max(...selectedObjects.map((object) => object.height)) + 40
                          const x = Math.min(...selectedObjects.map((object) => object.x)),
                            y = Math.min(...selectedObjects.map((object) => object.y))
                          view.execute(
                            cmdMoveObjects(
                              selectedObjects.map((object, index) => ({
                                id: object.id,
                                from: { x: object.x, y: object.y },
                                to: {
                                  x: x + (index % columns) * width,
                                  y: y + Math.floor(index / columns) * height,
                                },
                              })),
                            ),
                          )
                        }}
                      >
                        宫格布局
                      </button>
                      <button type="button" disabled title="智能布局尚未接入">
                        智能布局
                      </button>
                    </div>
                  </details>
                </>
              )}
              {surface === 'text' && selectedObject && (
                <details className="selection-menu">
                  <summary>背景色</summary>
                  <div>
                    {[
                      ['无颜色', undefined],
                      ['青绿色', '#cff2e9'],
                      ['靛蓝色', '#dce5ff'],
                      ['紫色', '#eadcff'],
                      ['橙色', '#ffe3cf'],
                      ['黄色', '#fff3bd'],
                    ].map(([label, color]) => (
                      <button
                        type="button"
                        key={label}
                        onClick={() =>
                          view.execute(
                            cmdUpdateObject(
                              selectedObject.id,
                              { textCard: Boolean(color), backgroundColor: color },
                              {
                                textCard: selectedObject.textCard,
                                backgroundColor: selectedObject.backgroundColor,
                              },
                            ),
                          )
                        }
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                </details>
              )}
              {(surface === 'image' || surface === 'video') && (
                <details className="selection-menu">
                  <summary>AI 助手</summary>
                  <div>
                    {['电影级镜头设计', '提示词反推', '来点灵感'].map((label) => (
                      <button
                        type="button"
                        key={label}
                        onClick={() => {
                          agentStore.setDraft(`${label}：请基于当前选中素材分析并提出建议。`)
                          setSideTab('chat')
                          setSidebarOpen(true)
                        }}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                </details>
              )}
              {(surface === 'image' || surface === 'video' || surface === 'text') &&
                selectedObject && (
                  <>
                    <ToolButton onClick={() => setPreviewObject(selectedObject)}>全屏</ToolButton>
                    <ToolButton
                      onClick={() => {
                        if (surface === 'text') {
                          const url = URL.createObjectURL(
                            new Blob([selectedObject.text || ''], {
                              type: 'text/plain;charset=utf-8',
                            }),
                          )
                          downloadUrl(url, `${selectedObject.name || '文本'}.txt`)
                          setTimeout(() => URL.revokeObjectURL(url), 1000)
                        } else {
                          const url = mediaSource(selectedObject)
                          if (url) {
                            downloadUrl(
                              url,
                              selectedObject.name ||
                                `${selectedObject.id}.${selectedObject.ext || (surface === 'video' ? 'mp4' : 'png')}`,
                            )
                          }
                        }
                      }}
                    >
                      下载
                    </ToolButton>
                  </>
                )}
              {surface !== 'none' && (
                <ToolButton
                  onClick={() => {
                    setSideTab('inspector')
                    setSidebarOpen(true)
                  }}
                >
                  更多
                </ToolButton>
              )}
              {(surface === 'multi' || surface === 'board') && (
                <ToolButton
                  disabled={!snapshot.capabilities.canEdit}
                  onClick={() => view.duplicateSelection()}
                >
                  复制
                </ToolButton>
              )}
            </div>
          )}
        {(snapshot.capabilities.canUndo || snapshot.capabilities.canRedo) && (
          <div className="selection-history" role="toolbar" aria-label="画布历史操作">
            <ToolButton disabled={!snapshot.capabilities.canUndo} onClick={() => view.undo()}>
              撤销
            </ToolButton>
            <ToolButton disabled={!snapshot.capabilities.canRedo} onClick={() => view.redo()}>
              重做
            </ToolButton>
          </div>
        )}
        {store.doc.order.length === 0 && (
          <div className="canvas-empty">
            <p className="canvas-empty-title">画布是空的</p>
            <p className="canvas-empty-hint">插入图片、视频、时间线占位符，或从素材库添加素材</p>
          </div>
        )}

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
              if (
                !event.nativeEvent.isComposing &&
                (event.metaKey || event.ctrlKey) &&
                event.key === 'Enter'
              ) {
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
              onClick={() => {
                view.focusObject(snapshot.selection[0] ?? '')
                setContextMenu(null)
              }}
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
            {(() => {
              const selected = snapshot.selection[0]
              const target = selected ? view.getDocument().objects[selected] : undefined
              if (
                !target?.lineage?.params ||
                !isDraftParams(target.lineage.params as Record<string, unknown>)
              ) {
                return null
              }
              return (
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    setContextMenu(null)
                    setGenerating(true)
                    void resubmitFullQuality(store, view, target)
                      .then((result) => setNotice(result.ok ? '已提交高配重出' : result.error))
                      .finally(() => setGenerating(false))
                  }}
                >
                  草稿高配重出
                </button>
              )
            })()}
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
        {surface === 'timeline' && !sidebarOpen && (
          <section
            className="selection-timeline"
            aria-label="选中时间线编辑器"
            style={{
              left: Math.max(12, Math.min(snapshot.viewport.width - 620, anchor.x - 300)),
              top: Math.max(12, Math.min(snapshot.viewport.height - 460, anchor.bottom)),
            }}
          >
            <TimelineStudio
              store={inlineTimelineStore}
              media={view.getDocument().order.map((id) => view.getDocument().objects[id])}
              active
              ensureSaved={saver.ensureSaved}
              exportKey={`gencanvas.export.${document.id}.${selectedObjectId}`}
            />
          </section>
        )}
        {previewObject && (
          <dialog
            ref={(node) => {
              if (node && !node.open) {
                node.showModal()
              }
            }}
            onCancel={() => setPreviewObject(null)}
            className="selection-preview"
            aria-modal="true"
            aria-label="媒体全屏预览"
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                setPreviewObject(null)
              }
            }}
          >
            <button type="button" autoFocus onClick={() => setPreviewObject(null)}>
              关闭预览
            </button>
            {previewObject.kind === 'text' ? (
              <p>{previewObject.text}</p>
            ) : previewObject.kind === 'video' ? (
              <video src={mediaSource(previewObject)} controls />
            ) : (
              <img src={mediaSource(previewObject)} alt={previewObject.name || '图片预览'} />
            )}
          </dialog>
        )}
        <ComposerPresence
          sidebarOpen={
            sidebarOpen ||
            !mediaSurface(surface) ||
            ['mask', 'inpaint', 'recognizing'].includes(snapshot.requestedTool)
          }
        >
          <form
            className="next-composer selection-composer"
            ref={composerRef}
            onChangeCapture={() => {
              draftDirty.current = true
            }}
            style={{
              left: Math.max(
                Math.min(composerWidth / 2 + 16, snapshot.viewport.width / 2),
                Math.min(
                  snapshot.viewport.width -
                    Math.min(composerWidth / 2 + 16, snapshot.viewport.width / 2),
                  anchor.x,
                ),
              ),
              top: Math.max(
                72,
                Math.min(
                  snapshot.viewport.height - composerHeight - 16,
                  Math.max(anchor.bottom, anchor.top + toolbarHeight + 12),
                ),
              ),
              bottom: 'auto',
            }}
            onSubmit={(event) => {
              event.preventDefault()
              if (sidebarOpen || !prompt.trim() || generating) {
                return
              }
              setGenerating(true)
              void submitFromComposer(store, view, prompt.trim(), generationKind, generationDraft, {
                draftMode,
                seed: lockedSeed(seedLocked, seedValue),
              })
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
            {selectedObject && (surface === 'image' || surface === 'video') && (
              <div className="selection-reference">
                <span>选中素材</span>
                {surface === 'image' ? (
                  <img src={mediaSource(selectedObject)} alt={selectedObject.name || '选中图片'} />
                ) : (
                  <video src={mediaSource(selectedObject)} controls preload="metadata" />
                )}
              </div>
            )}
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
              <span className="next-composer-kind">
                {generationKind === 'image' ? '图片' : '视频'}
              </span>
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
            <div className="next-composer-actions">
              <button
                type="button"
                className="next-composer-secondary"
                title="一次生成四个方向的图片"
                disabled={
                  sidebarOpen ||
                  generating ||
                  generationKind !== 'image' ||
                  !prompt.trim() ||
                  Object.values(store.doc.objects).some((object) => object.pendingConfirm)
                }
                onClick={() => {
                  setGenerating(true)
                  const size = outputSize(generationDraft)
                  void submitGroupGeneration({
                    docStore: store,
                    ensureSaved: saver.ensureSaved,
                    genParams: {
                      prompt: prompt.trim(),
                      kind: 'image',
                      model: generationDraft.model,
                      ...size,
                      steps: 20,
                      cfgScale: 7,
                      sampler: 'euler',
                      scheduler: 'normal',
                      referenceAssetIds: generationDraft.references.map((ref) => ref.assetId),
                    },
                  })
                    .then((result) => {
                      setNotice(
                        result.ok
                          ? '四方向生成已提交'
                          : 'error' in result
                            ? result.error
                            : '结果待确认，请查询原组结果',
                      )
                      saver.markDirty()
                    })
                    .catch((error: unknown) =>
                      setNotice(error instanceof Error ? error.message : '组生成失败'),
                    )
                    .finally(() => setGenerating(false))
                }}
              >
                四方向生成
              </button>
              <button
                type="button"
                className="next-composer-pill"
                aria-pressed={draftMode && generationKind === 'image'}
                disabled={generationKind === 'video'}
                title={
                  generationKind === 'video'
                    ? '视频尺寸为固定档位，暂不支持草稿模式'
                    : '草稿模式：低步数小尺寸先出小样，满意后右键高配重出'
                }
                onClick={() => setDraftMode((on) => !on)}
              >
                草稿
              </button>
              <button
                type="button"
                className="next-composer-pill"
                aria-pressed={seedLocked}
                title="锁定种子可复现构图；开启时自动取当前选中结果的种子，留空则随机"
                onClick={() => {
                  const locked = !seedLocked
                  if (locked && !seedValue.trim()) {
                    const seed = selectedSeed(store, snapshot.selection[0])
                    if (seed !== null) {
                      setSeedValue(String(seed))
                    }
                  }
                  setSeedLocked(locked)
                }}
              >
                种子
                {seedLocked && lockedSeed(true, seedValue) >= 0
                  ? `·${lockedSeed(true, seedValue)}`
                  : ''}
              </button>
              {seedLocked && (
                <input
                  className="next-composer-seed"
                  type="number"
                  aria-label="种子"
                  min={0}
                  step={1}
                  value={seedValue}
                  placeholder="随机"
                  title="锁定的种子值，留空则随机"
                  onChange={(event) => setSeedValue(event.target.value)}
                />
              )}
              <span className="next-composer-spacer" />
              <button
                type="button"
                className="next-composer-quiet"
                aria-expanded={promptLibraryOpen}
                title="提示词库：收藏与历史搜索"
                onClick={() => setPromptLibraryOpen((open) => !open)}
              >
                词库
              </button>
              <button
                type="button"
                className="next-composer-quiet"
                disabled={!prompt.trim()}
                title={isFavorite(favorites, prompt) ? '已在收藏中' : '收藏当前提示词'}
                onClick={() => setFavorites(addFavorite(localStorage, prompt))}
              >
                {isFavorite(favorites, prompt) ? '已收藏' : '收藏'}
              </button>
              <button
                type="submit"
                className="next-composer-submit"
                title="生成（⌘ / Ctrl + Enter）"
                disabled={!prompt.trim() || generating || !generationDraft.model}
              >
                {generating ? '提交中…' : draftMode ? '生成小样' : '生成'}
              </button>
            </div>
            {promptLibraryOpen && (
              <PromptLibrary
                query={promptQuery}
                onQuery={setPromptQuery}
                favorites={favorites}
                recent={recentPrompts(store.doc.generations)}
                onUse={(text) => {
                  draftDirty.current = true
                  setPrompt(text)
                  setPromptLibraryOpen(false)
                }}
                onRemove={(id) => setFavorites(removeFavorite(localStorage, id))}
              />
            )}
          </form>
        </ComposerPresence>
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
      <SidebarPresence open={sidebarOpen}>
        <header className="chat-head">
          <div className="chat-heading">
            <b>{sideTab === 'chat' ? '创作对话' : '画布面板'}</b>
            {sideTab !== 'chat' && (
              <span className="chat-head-hint">想法、过程与作品，都在这里</span>
            )}
          </div>
          <button
            className="icon-btn"
            type="button"
            ref={panelCloseButton}
            aria-label="收起侧栏"
            onClick={() => setSidebarOpen(false)}
          >
            ×
          </button>
        </header>
        {sideTab !== 'chat' && (
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
        )}
        <div className="next-sidebar-content">
          <SidebarContent
            tab={sideTab}
            store={store}
            view={view}
            selection={snapshot.selection}
            onConfigureMedia={configureMedia}
            agentStore={agentStore}
            active={sidebarOpen}
            timelineNodeId={timelineNodeId}
            ensureSaved={saver.ensureSaved}
            onSvg={setSvgObject}
          />
        </div>
      </SidebarPresence>
      <button
        ref={chatTrigger}
        className="chat-expand"
        type="button"
        aria-label="与 AI 对话"
        aria-expanded={sidebarOpen && sideTab === 'chat'}
        onClick={() => {
          setSideTab('chat')
          setSidebarOpen(true)
        }}
      >
        <svg
          className="chat-expand-icon"
          width="20"
          height="20"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.6"
          aria-hidden="true"
        >
          <path d="M20 11a8 8 0 01-8 8H5l-3 2V11a8 8 0 018-8h2M7 11h6M7 15h4" />
        </svg>
        <span className="chat-expand-label" aria-hidden="true">
          与 AI 对话
        </span>
      </button>
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
  agentStore,
  onConfigureMedia,
  active,
  timelineNodeId,
  ensureSaved,
  onSvg,
}: {
  tab: 'inspector' | 'library' | 'chat' | 'storyboard' | 'timeline'
  store: DocStore
  view: ReturnType<typeof useCanvasView>
  selection: readonly string[]
  agentStore: AgentStore
  active: boolean
  timelineNodeId?: string
  ensureSaved: () => Promise<unknown>
  onSvg: (object: CanvasObj) => void
  onConfigureMedia: (object: CanvasObj) => void
}) {
  const object = selection.length === 1 ? view.getDocument().objects[selection[0]] : undefined
  const targetTimelineId =
    timelineNodeId && store.doc.objects[timelineNodeId]?.placeholderType === 'timeline'
      ? timelineNodeId
      : undefined
  const scopedTimeline = useMemo(
    () => timelineNodeStore(store, targetTimelineId),
    [store, targetTimelineId],
  )
  const [interactionError, setInteractionError] = useState('')
  if (tab === 'inspector') {
    if (!object) {
      return <p className="next-panel-empty">选择一个对象查看属性</p>
    }
    return (
      <div className="next-panel-body">
        <h2>{object.name || object.kind}</h2>
        {object.kind === 'image' && (object.src || object.assetId) && (
          <button type="button" onClick={() => onSvg(object)}>
            导出 SVG
          </button>
        )}
        <details>
          <summary>连线与引用</summary>
          {documentConnections(store.doc.objects)
            .filter((edge) => edge.sourceId === object.id || edge.targetId === object.id)
            .map((edge) => (
              <div key={`${edge.kind}:${edge.sourceId}:${edge.targetId}`}>
                <p>
                  {store.doc.objects[edge.sourceId]?.name || '素材'} →{' '}
                  {store.doc.objects[edge.targetId]?.name || '节点'}（
                  {edge.kind === 'lineage' ? '生成来源' : '参考素材'}）
                </p>
                {edge.kind === 'reference' && (
                  <button
                    type="button"
                    disabled={Boolean(
                      store.doc.objects[edge.targetId]?.nodeRun ||
                      store.doc.objects[edge.targetId]?.generationId,
                    )}
                    onClick={() => {
                      try {
                        detachReference(store, edge.targetId, edge.sourceId)
                        setInteractionError('')
                      } catch (error) {
                        setInteractionError(error instanceof Error ? error.message : '断开失败')
                      }
                    }}
                  >
                    断开引用
                  </button>
                )}
              </div>
            ))}
          <p role="status">{interactionError}</p>
        </details>

        {object.kind === 'text' && (
          <label>
            文字内容
            <textarea
              aria-label="编辑文字内容"
              value={object.text ?? ''}
              onChange={(event) =>
                view.execute(
                  cmdUpdateObject(object.id, { text: event.target.value }, { text: object.text }),
                )
              }
            />
          </label>
        )}
        {(object.kind === 'image' || object.kind === 'video' || object.mediaType) && (
          <>
            <p>{object.mediaType === 'video' || object.kind === 'video' ? '视频' : '图片'}节点</p>
            <button type="button" onClick={() => onConfigureMedia(object)}>
              编辑生成参数
            </button>
          </>
        )}
        {object.kind === 'board' && (
          <label>
            画板颜色
            <input
              type="color"
              aria-label="画板颜色"
              value={object.backgroundColor || '#ffffff'}
              onChange={(event) =>
                view.execute(
                  cmdUpdateObject(
                    object.id,
                    { backgroundColor: event.target.value },
                    { backgroundColor: object.backgroundColor },
                  ),
                )
              }
            />
          </label>
        )}

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
      <AgentPanel
        store={agentStore}
        history={store.doc.chat ?? []}
        getSelection={() => view.getSnapshot().selection}
      />
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
  return (
    <TimelineStudio
      key={targetTimelineId ?? 'document'}
      store={scopedTimeline}
      media={Object.values(view.getDocument().objects)}
      active={active}
      ensureSaved={ensureSaved}
      exportKey={`gencanvas.export.${store.doc.id}.${targetTimelineId ?? 'document'}`}
    />
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

  const [uploadError, setUploadError] = useState('')
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
      {uploadError && <p role="alert">{uploadError}</p>}
      <label className="next-upload-button">
        上传素材
        <input
          type="file"
          accept="image/*,video/*"
          onChange={(event) => {
            const file = event.target.files?.[0]
            event.target.value = ''
            setUploadError('')
            if (!file) {
              return
            }
            const kind = file.type.startsWith('video/') ? 'video' : 'image'
            const ext = file.name.split('.').pop() || (kind === 'video' ? 'mp4' : 'png')
            void fetch(
              `/api/assets?kind=${kind}&ext=${encodeURIComponent(ext)}&name=${encodeURIComponent(file.name)}`,
              { method: 'POST', headers: { 'Content-Type': file.type }, body: file },
            )
              .then((response) => {
                if (!response.ok) {
                  throw new Error('素材上传失败，请重试')
                }
                return response.json()
              })
              .then(
                (body: { asset?: { id: string; ext: string }; urls?: { original?: string } }) => {
                  if (!body.asset || !body.urls?.original) {
                    throw new Error('上传结果缺少素材地址')
                  }
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
              .catch((cause: unknown) =>
                setUploadError(cause instanceof Error ? cause.message : '素材上传失败'),
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
  options: { draftMode: boolean; seed: number },
) {
  const selected = view.getSnapshot().selection[0]
  const source = selected ? view.getDocument().objects[selected] : undefined
  const initImage =
    source && source.kind === 'image' ? { dataUrl: await objectToDataUrl(source) } : undefined
  const isEmptyMedia = source?.placeholderType === 'image' || source?.placeholderType === 'video'
  const anchor = source
    ? { x: isEmptyMedia ? source.x : source.x + source.width + 40, y: source.y }
    : view.screenToWorld({ x: 180, y: 180 })
  const size = options.draftMode ? draftSize(outputSize(draft)) : outputSize(draft)
  const genParams = {
    prompt,
    negativePrompt: '',
    model: draft.model,
    width: size.width,
    height: size.height,
    steps: options.draftMode ? DRAFT_STEPS : 20,
    cfgScale: 7,
    seed: options.seed,
    sampler: 'euler',
    scheduler: 'normal',
    denoise: initImage ? draft.denoise : 1,
    kind,
    durationSec: draft.durationSec,
    fps: 16,
    referenceAssetIds: initImage ? [] : draft.references.map((reference) => reference.assetId),
  }
  return submitGeneration({
    docStore: store,
    genParams,
    anchor,
    initImage,
    objectId: isEmptyMedia ? source?.id : undefined,
    lineage:
      source && !isEmptyMedia
        ? { fromId: source.id, params: genParams }
        : draft.references.length
          ? { fromId: '', params: { ...genParams, references: draft.references } }
          : undefined,
  })
}

/** 草稿小样满意后的原参数高配重出:同提示词同种子,恢复默认步数与原尺寸。 */
async function resubmitFullQuality(
  store: DocStore,
  view: ReturnType<typeof useCanvasView>,
  obj: CanvasObj,
) {
  const lineage = obj.lineage!
  const params = fullQualityParams(lineage.params as Record<string, unknown>)
  const source = lineage.fromId ? view.getDocument().objects[lineage.fromId] : undefined
  const initImage =
    source && source.kind === 'image' ? { dataUrl: await objectToDataUrl(source) } : undefined
  return submitGeneration({
    docStore: store,
    genParams: params,
    anchor: { x: obj.x + obj.width + 40, y: obj.y },
    initImage,
    lineage: { fromId: lineage.fromId || obj.id, params },
  })
}

/** 选中结果实际使用的种子:优先完成实体里固化的种子,退回谱系参数。 */
function selectedSeed(store: DocStore, objectId: string | undefined): number | null {
  const obj = objectId ? store.doc.objects[objectId] : undefined
  const entity = obj?.generationId ? store.doc.generations?.[obj.generationId] : undefined
  return seedOf(entity?.result) ?? seedOf(obj?.lineage?.params)
}

function PromptLibrary({
  query,
  onQuery,
  favorites,
  recent,
  onUse,
  onRemove,
}: {
  query: string
  onQuery: (value: string) => void
  favorites: PromptFavorite[]
  recent: string[]
  onUse: (prompt: string) => void
  onRemove: (id: string) => void
}) {
  const keyword = query.trim().toLowerCase()
  const match = (text: string) => !keyword || text.toLowerCase().includes(keyword)
  const favoriteTexts = new Set(favorites.map((item) => item.prompt))
  const favoriteItems = favorites.filter((item) => match(item.prompt))
  const recentItems = recent.filter((text) => match(text) && !favoriteTexts.has(text))
  return (
    <div className="next-prompt-library" aria-label="提示词库">
      <input
        aria-label="搜索提示词"
        value={query}
        placeholder="搜索收藏与历史…"
        onChange={(event) => onQuery(event.target.value)}
      />
      <strong>收藏</strong>
      {favoriteItems.length === 0 && <p className="next-panel-empty">暂无收藏</p>}
      {favoriteItems.map((item) => (
        <div className="next-prompt-row" key={item.id}>
          <button type="button" className="next-prompt-use" onClick={() => onUse(item.prompt)}>
            {item.prompt}
          </button>
          <button type="button" onClick={() => onRemove(item.id)}>
            移除
          </button>
        </div>
      ))}
      <strong>最近</strong>
      {recentItems.length === 0 && <p className="next-panel-empty">暂无历史</p>}
      {recentItems.map((text) => (
        <div className="next-prompt-row" key={text}>
          <button type="button" className="next-prompt-use" onClick={() => onUse(text)}>
            {text}
          </button>
        </div>
      ))}
    </div>
  )
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
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { active?: boolean }) {
  const label = typeof props.children === 'string' ? props.children : ''
  const paths: Record<string, string> = {
    选择: 'M5 3l14 9-7 1-3 7z',
    抓手: 'M8 13V6a2 2 0 014 0v6M12 6V4a2 2 0 014 0v8M16 8a2 2 0 014 0v7c0 4-3 7-7 7-3 0-5-2-7-5l-3-4a2 2 0 013-2l2 2',
    连线: 'M5 5h5v5H5zM14 14h5v5h-5zM10 7h7v7',
    文字: 'M4 5h16M12 5v15M8 20h8',
    抠图: 'M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5M8 12l3 3 5-6',
    重绘: 'M4 20l4-1L20 7l-3-3L5 16zM14 7l3 3',
    撤销: 'M9 5L4 10l5 5M4 10h9a6 6 0 016 6',
    重做: 'M15 5l5 5-5 5M20 10h-9a6 6 0 00-6 6',
    删除: 'M4 6h16M9 6V3h6v3M6 6l1 14h10l1-14M10 10v6M14 10v6',
  }
  return (
    <button
      className={['canvas-tooltip', active ? 'is-active' : '', className].filter(Boolean).join(' ')}
      type="button"
      {...props}
      aria-label={label || props['aria-label']}
    >
      {paths[label] ? (
        <>
          <svg
            width="20"
            height="20"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.7"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <path d={paths[label]} />
          </svg>
          <span className="tool-button-caption">{label}</span>
        </>
      ) : (
        props.children
      )}
    </button>
  )
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
  return '已保存'
}

const root = document.getElementById('canvas-root')
if (!root) {
  throw new Error('缺少 canvas-root')
}
createRoot(root).render(
  <StrictMode>
    <CanvasApp />
  </StrictMode>,
)
