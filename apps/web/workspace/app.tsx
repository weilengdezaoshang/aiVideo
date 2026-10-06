import { installSessionFetch } from '../shared/session-fetch.js'
installSessionFetch()

import { StrictMode, useCallback, useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Sidebar, type WorkspaceTab } from '../ui/Sidebar.js'
import { Button, Dialog } from '../ui/primitives.js'
import { type Project } from '../ui/media-cards.js'
import type { Capabilities, MediaKind, NodeDraft } from '../canvas/state/node-model.js'
import {
  CreationIntent,
  getCapabilities,
  getDocuments,
  json,
  request,
  validateGeneration,
  type CanvasDocument,
} from './api.js'
import { HomePage } from './HomePage.js'
import { SettingsDialog } from './SettingsDialog.js'
import { AssetLibrary } from './AssetLibrary.js'
import { CanvasLibrary } from './CanvasLibrary.js'

function currentTab(): WorkspaceTab {
  const tab = new URLSearchParams(location.search).get('tab')
  return tab === 'canvas' || tab === 'assets' ? tab : 'overview'
}
function WorkspaceApp() {
  const [settingsEnabled, setSettingsEnabled] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    void request<{ settingsEnabled: boolean }>('/api/runtime', { signal: controller.signal })
      .then((runtime) => {
        if (!controller.signal.aborted) {
          setSettingsEnabled(runtime.settingsEnabled === true)
        }
      })
      .catch(() => {})
    return () => controller.abort()
  }, [])
  const [tab, setTab] = useState(currentTab)
  useEffect(() => {
    document.title = `${tab === 'canvas' ? '我的画布' : tab === 'assets' ? '我的素材' : '创作首页'} · 帧屿集 FRAYUNE`
  }, [tab])
  const [projects, setProjects] = useState<Project[]>([])
  const [loading, setLoading] = useState(true)
  const [projectsError, setProjectsError] = useState('')
  const [caps, setCaps] = useState<Capabilities | null>(null)
  const [capsError, setCapsError] = useState('')
  const [refresh, setRefresh] = useState(0)
  const [configVersion, setConfigVersion] = useState(0)
  const [busy, setBusy] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [modal, setModal] = useState<string | null>(null)
  const [editing, setEditing] = useState<{ project: Project; action: 'rename' | 'delete' } | null>(
    null,
  )
  const [name, setName] = useState('')
  const [mutationError, setMutationError] = useState('')
  const [mutating, setMutating] = useState(false)
  const intent = useRef<CreationIntent | null>(null)
  const requestBusy = useRef(false)
  const loadProjects = useCallback(() => setRefresh((value) => value + 1), [])
  useEffect(() => {
    const pop = () => setTab(currentTab())
    window.addEventListener('popstate', pop)
    return () => window.removeEventListener('popstate', pop)
  }, [])
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setProjectsError('')
    void getDocuments(controller.signal)
      .then(async ({ documents }) => {
        if (controller.signal.aborted) {
          return
        }
        setProjects(documents)
        setLoading(false)
        // Read covers in bounded batches; example art never impersonates user output.
        const covers = tab === 'canvas' ? documents : documents.slice(0, 3)
        for (let offset = 0; offset < covers.length; offset += 6) {
          if (controller.signal.aborted) {
            break
          }
          await Promise.all(
            covers.slice(offset, offset + 6).map(async (project) => {
              try {
                const { document } = await request<{ document: CanvasDocument }>(
                  `/api/documents/${project.id}`,
                  { signal: controller.signal },
                )
                const object = document.order
                  .map((id) => document.objects[id])
                  .find((item) => item?.kind === 'image' && (item.src || item.assetId))
                const thumbnail = object?.assetId
                  ? `/assets/${object.assetId}/${object.hasThumbs ? 't1024.webp' : `original.${object.ext || 'png'}`}`
                  : object?.src
                if (!controller.signal.aborted && thumbnail) {
                  setProjects((items) =>
                    items.map((item) => (item.id === project.id ? { ...item, thumbnail } : item)),
                  )
                }
              } catch {
                /* Missing cover does not hide a usable document. */
              }
            }),
          )
        }
      })
      .catch((err) => {
        if (!controller.signal.aborted) {
          setProjectsError(err instanceof Error ? err.message : '项目读取失败')
          setLoading(false)
        }
      })
    return () => controller.abort()
  }, [refresh, tab])
  useEffect(() => {
    const controller = new AbortController()
    setCapsError('')
    setCaps(null)
    void getCapabilities(controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) {
          setCaps(value)
        }
      })
      .catch((err) => {
        if (!controller.signal.aborted) {
          setCapsError(String(err))
        }
      })
    return () => controller.abort()
  }, [configVersion])
  function navigate(next: WorkspaceTab) {
    history.pushState(null, '', `/workspace?tab=${next}`)
    setTab(next)
    loadProjects()
    window.scrollTo(0, 0)
  }
  async function create(kind?: MediaKind, draft?: NodeDraft) {
    if (requestBusy.current) {
      return
    }
    if (!intent.current && kind && draft) {
      if (!caps) {
        setError('模型尚未就绪')
        return
      }
      const reason = validateGeneration(kind, draft, caps)
      if (reason) {
        setError(reason)
        return
      }
    }
    if (!intent.current) {
      intent.current = new CreationIntent(kind, draft)
    }
    requestBusy.current = true
    setBusy(true)
    setPending(true)
    setError('')
    try {
      const id = await intent.current.run()
      location.assign(`/canvas?doc=${encodeURIComponent(id)}`)
    } catch (err) {
      setError(
        `${err instanceof Error ? err.message : '请求未完成'}。重试会沿用同一请求，不会重复创建。`,
      )
      setBusy(false)
      requestBusy.current = false
      loadProjects()
    }
  }
  function newCanvas() {
    if (pending) {
      setError('请先重试或打开上一次请求对应的画布。')
      return
    }
    void create()
  }
  async function mutateProject() {
    if (!editing || mutating) {
      return
    }
    setMutating(true)
    setMutationError('')
    try {
      const id = encodeURIComponent(editing.project.id)
      await request(
        `/api/documents/${id}${editing.action === 'rename' ? '/rename' : ''}`,
        editing.action === 'rename'
          ? { ...json({ name: name.trim() }), method: 'PUT' }
          : { method: 'DELETE' },
      )
      setEditing(null)
      loadProjects()
    } catch (err) {
      setMutationError(String(err))
    } finally {
      setMutating(false)
    }
  }
  const edit = (project: Project, action: 'rename' | 'delete') => {
    setEditing({ project, action })
    setName(project.name)
    setMutationError('')
  }
  return (
    <>
      <a href="#workspace-main" className="skip-link">
        跳至主要内容
      </a>
      <Sidebar
        tab={tab}
        onNavigate={navigate}
        onCreate={newCanvas}
        busy={busy}
        onHelp={() => setModal('help')}
        onSettings={settingsEnabled ? () => setModal('settings') : undefined}
      />
      <main id="workspace-main" className="workspace-main" tabIndex={-1}>
        {error && (
          <div className="request-alert" role="alert">
            <span>{error}</span>
            {intent.current && (
              <Button onClick={() => void create()} disabled={busy}>
                重试同一请求
              </Button>
            )}
            {intent.current?.documentId && (
              <a href={`/canvas?doc=${encodeURIComponent(intent.current.documentId)}`}>
                打开该画布
              </a>
            )}
          </div>
        )}
        {tab === 'overview' && (
          <HomePage
            caps={caps}
            capsError={capsError}
            onReloadCaps={() => setConfigVersion((value) => value + 1)}
            projects={projects}
            loading={loading}
            projectsError={projectsError}
            onReloadProjects={loadProjects}
            onCreate={newCanvas}
            onAll={() => navigate('canvas')}
            onRename={(project) => edit(project, 'rename')}
            onDelete={(project) => edit(project, 'delete')}
            onGenerate={(kind, draft) => void create(kind, draft)}
            busy={busy}
            pending={pending}
            onTool={setModal}
          />
        )}
        {tab === 'canvas' && (
          <CanvasLibrary
            projects={projects}
            loading={loading}
            error={projectsError}
            busy={busy || pending}
            onCreate={newCanvas}
            onReload={loadProjects}
            onRename={(project) => edit(project, 'rename')}
            onDelete={(project) => edit(project, 'delete')}
          />
        )}
        {tab === 'assets' && <AssetLibrary onCreate={() => navigate('overview')} />}
      </main>
      {settingsEnabled && modal === 'settings' ? (
        <SettingsDialog
          onClose={() => setModal(null)}
          onSaved={() => setConfigVersion((value) => value + 1)}
        />
      ) : (
        modal && (
          <Dialog title={modal === 'help' ? '开始你的创作' : modal} onClose={() => setModal(null)}>
            <div className="help-copy">
              {modal === 'help' ? (
                <>
                  <p>
                    输入画面描述，选择模型与画面比例，点击「生成」。作品会在画布中生成，你可以继续编辑、下载和整理。
                  </p>
                  <p>图生视频需要一张首帧参考图。切换后端后，模型与可用参数会自动更新。</p>
                  <p>灵感作品可点击查看、搜索和复用提示词。快捷键：⌘ / Ctrl + Enter 提交。</p>
                </>
              ) : (
                <>
                  <p>
                    {modal === '智能抠图'
                      ? '在画布中上传或选中一张图片，选择「智能抠图」，检查识别蒙版后确认。'
                      : '在画布中选中图片，打开「局部重绘」，涂抹需要修改的区域并描述新内容。'}
                  </p>
                  <p>可以继续已有画布，或新建画布导入图片。</p>
                  <div className="dialog-actions">
                    <Button
                      onClick={() => {
                        setModal(null)
                        navigate('canvas')
                      }}
                    >
                      我的画布
                    </Button>
                    <Button variant="primary" disabled={busy || pending} onClick={newCanvas}>
                      新建画布
                    </Button>
                  </div>
                </>
              )}
            </div>
          </Dialog>
        )
      )}
      {editing && (
        <Dialog
          title={editing.action === 'rename' ? '重命名画布' : '删除画布'}
          onClose={() => {
            if (!mutating) {
              setEditing(null)
            }
          }}
        >
          <form
            onSubmit={(event) => {
              event.preventDefault()
              void mutateProject()
            }}
          >
            {editing.action === 'rename' ? (
              <label className="rename-label">
                画布名称
                <input
                  autoFocus
                  aria-label="画布名称"
                  value={name}
                  maxLength={100}
                  disabled={mutating}
                  onChange={(event) => setName(event.target.value)}
                />
              </label>
            ) : (
              <p>删除「{editing.project.name}」？画布无法恢复，已生成的素材仍会保留。</p>
            )}
            {mutationError && (
              <p role="alert" className="inline-error">
                {mutationError}
              </p>
            )}
            <div className="dialog-actions">
              <Button disabled={mutating} onClick={() => setEditing(null)}>
                取消
              </Button>
              <Button
                type="submit"
                variant={editing.action === 'delete' ? 'danger' : 'primary'}
                disabled={mutating || (editing.action === 'rename' && !name.trim())}
              >
                {mutating ? '处理中…' : '确认'}
              </Button>
            </div>
          </form>
        </Dialog>
      )}
    </>
  )
}
const root = document.getElementById('workspace-root')
if (root) {
  createRoot(root).render(
    <StrictMode>
      <WorkspaceApp />
    </StrictMode>,
  )
}
