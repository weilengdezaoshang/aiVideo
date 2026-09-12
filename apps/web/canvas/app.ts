import { createNodePanel } from './flows/node-panel.js'
import { createNode, migrateNodes } from './state/node-model.js'
import { initCreateMenu } from './flows/create-menu.js'
// GenCanvas 画布入口(ESM):组装引擎、文档状态层、自动保存、底部输入区、
// 右侧对话栏、四方向组生成、抠图状态机与创作助手(PRD v0.4)。

import { createCanvasEngine, type Cam } from './engine/stage.js'
import { createDocStore, type DocStore, type CanvasDocData } from './state/doc-store.js'
import { createAutoSaver, type AutoSaver } from './state/autosave.js'
import {
  openDocument,
  renameDocument,
  DocumentMissingError,
  loadLocalDraft,
  clearLocalDraft,
  resolveDraftOnOpen,
} from './state/document-api.js'
import { createObjectLayer, type ObjectLayer } from './engine/object-layer.js'
import { connectGenerationEvents, type GenerationEventsHandle } from './state/generation-events.js'
import { submitGeneration, cancelPlaceholder } from './flows/generate.js'
import {
  submitGroupGeneration,
  regenerateGroup,
  confirmPendingGroup,
} from './flows/group-generate.js'
import { initContextMenu, type ContextMenu } from './flows/context-menu.js'
import { createInpaintSession, type InpaintSession } from './flows/inpaint.js'
import { watchDuplicateTabs } from './state/duplicate-tabs.js'
import { initLibrary, type LibraryDrawer } from './flows/library.js'
import { initSettings } from './settings.js'
import { findFreePosition, variantRowAnchors, clampIntoRect } from './state/placement.js'
import { createCutoutSession } from './flows/cutout.js'
import { createChat, type Chat } from './flows/chat.js'
import { createAgentFlow, type AgentFlow } from './flows/agent.js'
import { initObjectActions, type ObjectActions } from './flows/object-actions.js'
import { initThemeToggle } from './theme.js'
import { objectToDataUrl } from './flows/asset-util.js'
import { cmdAddObjects, cmdUpdateObject, cmdRemoveObjects } from './state/commands.js'
import {
  fillSelect,
  readReferenceWeight,
  resolveComposerReference,
  videoDurationOptions,
  videoFormatOptions,
} from './flows/composer-reference.js'
import type { Capability } from './state/node-model.js'

/** 输入卡创作模式;视频模式按能力契约门控(技术方案 v0.1 §5.1) */
type ComposerMode = 'generate' | 'video' | 'agent'

const $ = <T extends HTMLElement = HTMLElement>(sel: string): T => document.querySelector(sel) as T

const rootEl = $('#canvas-root')
const surfaceEl = $('#canvas-surface')
const composerInput = $('#composer-input') as HTMLTextAreaElement
const canvasEmpty = $('#canvas-empty')
const zoomValue = $('#zoom-value')
const saveStatusEl = $('#save-status')
const docNameInput = $('#doc-name') as HTMLInputElement

/** 轻提示(不阻塞操作,只确认"已开始/已保留"类反馈) */
let toastTimer: ReturnType<typeof setTimeout> | null = null
function toast(text: string) {
  const el = $('#toast')
  el.textContent = text
  el.hidden = false
  if (toastTimer) {
    clearTimeout(toastTimer)
  }
  toastTimer = setTimeout(() => {
    el.hidden = true
  }, 2200)
}

initThemeToggle($('#theme-toggle') as HTMLButtonElement)

const userMenu = $('#user-menu') as HTMLDetailsElement
$('#user-menu-settings').addEventListener('click', () => {
  userMenu.open = false
})
$('#user-menu-shortcuts').addEventListener('click', () => {
  userMenu.open = false
  toast('快捷键：空格拖动画布，⌘ / Ctrl + Enter 发送，Delete 删除选中内容')
})

let objectLayer: ObjectLayer | null = null
/** 当前选中(Transient) */
let selection: string[] = []
let objectActions: ObjectActions | null = null
let nodePanel: ReturnType<typeof createNodePanel> | null = null
let cutoutSession: ReturnType<typeof createCutoutSession> | null = null
let inpaint: InpaintSession | null = null
let chat: Chat | null = null
/** 素材库抽屉(导入资产入口) */
let libraryDrawer: LibraryDrawer | null = null
/** 微任务合并去重标记;引擎构造即触发 onCamMove,须先于引擎声明 */
let frameQueued = false
/** 正在编辑的文字对象 id */
let editingTextId: string | null = null

const engine = createCanvasEngine(rootEl, surfaceEl, {
  onCamMove: (cam: Cam) => {
    zoomValue.textContent = `${Math.round(cam.scale * 100)}%`
    objectLayer?.recomputeVisibility()
    if (editingTextId) {
      commitTextEdit()
    }
    queueFrame(() => objectActions?.refresh())
    nodePanel?.refresh()
  },
})
// 初始视野:世界原点居中;文档加载后若已有对象会重定位
engine.centerOn({ x: 0, y: 0 }, 1)

/** 微任务合并高频刷新(隐藏标签页里 rAF 不触发,会让操作栏位置卡死)。 */
function queueFrame(fn: () => void) {
  if (frameQueued) {
    return
  }
  frameQueued = true
  queueMicrotask(() => {
    frameQueued = false
    fn()
  })
} // 缩放控件:以视口中心为锚
const viewportCenter = () => ({ x: engine.stage.width() / 2, y: engine.stage.height() / 2 })
$('#canvas-tool-toggle').addEventListener('click', () => {
  engine.setPanTool(engine.interaction.getState().mode !== 'pan')
})
engine.interaction.subscribe((state) => {
  const button = $('#canvas-tool-toggle') as HTMLButtonElement
  const pan = state.effective === 'pan'
  button.disabled = state.mode === 'mask' || state.mode === 'recognizing'
  button.setAttribute('aria-pressed', String(pan))
  button.setAttribute('aria-label', pan ? '切换为选择工具' : '切换为拖动工具')
  button.title = pan ? '拖动工具 · 点击切换为选择' : '选择工具 · 点击切换为拖动'
  for (const id of ['tool-upload', 'tool-board', 'tool-text', 'library-open']) {
    const action = document.getElementById(id) as HTMLButtonElement | null
    if (action) {
      action.disabled = state.effective !== 'select'
    }
  }
  objectActions?.refresh()
})
const zoomMenu = $('#zoom-menu') as HTMLDetailsElement
document.querySelectorAll('[data-zoom-scale]').forEach((button) => {
  button.addEventListener('click', () => {
    if (!engine.interaction.canNavigate()) {
      return
    }
    engine.zoomToScale(Number(button.getAttribute('data-zoom-scale')), viewportCenter())
    zoomMenu.open = false
  })
})
$('#zoom-fit').addEventListener('click', () => {
  if (docStore) {
    fitToContent(docStore.doc)
  }
  zoomMenu.open = false
})
/* ---------- 保存状态 + 冲突(PRD §9) ---------- */

function updateSaveStatus(
  status: 'idle' | 'saving' | 'saved' | 'error' | 'conflict',
  detail?: string,
) {
  saveStatusEl.classList.toggle('pending', status === 'saving')
  saveStatusEl.classList.toggle('error', status === 'error')
  if (status === 'saving') {
    saveStatusEl.textContent = '保存中…'
  } else if (status === 'saved') {
    saveStatusEl.textContent = '已保存'
  } else if (status === 'error') {
    saveStatusEl.textContent = '保存失败'
    if (detail) {
      saveStatusEl.title = detail
    }
  } else if (status === 'conflict') {
    saveStatusEl.textContent = '有冲突'
    $('#conflict-banner').hidden = false
  } else {
    saveStatusEl.textContent = '已保存'
  }
}

$('#conflict-reload').addEventListener('click', () => location.reload())
$('#conflict-dismiss').addEventListener('click', () => {
  $('#conflict-banner').hidden = true
  autoSaver?.clearConflict()
  updateSaveStatus('error', '存在未解决的保存冲突')
})

/* ---------- 画布名称:失焦/回车提交重命名(只动名称,不覆盖内容) ---------- */

let renameTimer: ReturnType<typeof setTimeout> | null = null
docNameInput.addEventListener('change', () => {
  if (!docStore) {
    return
  }
  const store = docStore
  const name = docNameInput.value.trim() || '未命名画布'
  if (renameTimer) {
    clearTimeout(renameTimer)
  }
  renameTimer = setTimeout(async () => {
    try {
      const doc = await renameDocument(store.doc.id, name)
      store.doc.name = doc.name
      store.doc.revision = doc.revision
      docNameInput.value = doc.name
      updateSaveStatus('saved')
    } catch (err) {
      toast(`重命名失败:${err instanceof Error ? err.message : String(err)}`)
    }
  }, 400)
})
docNameInput.addEventListener('keydown', (e: KeyboardEvent) => {
  if (e.key === 'Enter') {
    e.preventDefault()
    docNameInput.blur()
  }
})

/* ---------- 文档启动(?doc= 严格打开 / 旧入口回退) ---------- */

let docStore: DocStore | null = null
let autoSaver: AutoSaver | null = null
let agentFlow: AgentFlow | null = null
let centeredOnce = false

/** 初始视野:框住全部内容并标记完成。
 * 舞台布局未收敛(尺寸 0×0)时 fitToContent 拿到的是负比例,会被钳到 5%,
 * 因此失败不标记,等 onSizeChanged(ResizeObserver/启动补偿帧)重放到成功为止。 */
function fitInitialView() {
  const doc = docStore?.doc
  if (centeredOnce || !doc || doc.order.length === 0) {
    return
  }
  if (fitToContent(doc)) {
    centeredOnce = true
  }
}
engine.onSizeChanged(fitInitialView)

/** 等首轮数据、缩略图和画布绘制稳定后再揭示界面，避免刷新时空画布跳变。 */
async function finishCanvasBoot(doc: CanvasDocData | null = null) {
  const overlay = $('#canvas-boot')
  if (!overlay || overlay.classList.contains('is-ready')) {
    return
  }
  const urls = doc
    ? Object.values(doc.objects || {})
        .filter((obj) => (obj.kind === 'image' || obj.kind === 'video') && (obj.assetId || obj.src))
        .map((obj) =>
          obj.assetId
            ? `/assets/${obj.assetId}/${obj.hasThumbs === false ? `original.${obj.ext || 'png'}` : 't256.webp'}`
            : obj.src,
        )
        .filter((url): url is string => Boolean(url))
    : []
  const imagesReady = Promise.allSettled(
    urls.map(
      (url) =>
        new Promise<void>((resolve) => {
          const image = new Image()
          image.onload = image.onerror = () => resolve()
          image.src = String(url)
        }),
    ),
  )
  await Promise.race([imagesReady, new Promise((resolve) => setTimeout(resolve, 1500))])
  // 双 rAF 等一帧再揭示;后台标签页 rAF 不触发(见 stage.ts 同类处理),超时兜底防遮罩卡死
  await Promise.race([
    new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))),
    new Promise((resolve) => setTimeout(resolve, 400)),
  ])
  overlay.classList.add('is-ready')
  setTimeout(() => overlay.remove(), 200)
}

/** 文档不存在 / 读取失败:明确提示,不用新建兜底(PRD §3)。 */
function showDocProblem(title: string, hint: string) {
  const overlay = $('#doc-missing')
  $('#doc-missing-title').textContent = title
  $('#doc-missing-hint').textContent = hint
  $('#doc-draft-recover').hidden = true
  $('#doc-server-open').hidden = true
  overlay.hidden = false
  $('#composer').hidden = true
  void finishCanvasBoot()
}

/**
 * 打开成功但存在与服务端不一致的未确认草稿:让用户显式选择(恢复草稿/打开服务端版本)。
 * 两个方向都会保留草稿本身;草稿只在保存确认(或内容被服务端包含)时清除。
 */
function showDraftChoice(doc: CanvasDocData, draft: CanvasDocData) {
  const overlay = $('#doc-missing')
  $('#doc-missing-title').textContent = '发现未保存的本机草稿'
  $('#doc-missing-hint').textContent =
    '服务器上的版本与本机草稿不同,草稿可能包含尚未保存的修改(如上次保存失败或中断)。可恢复草稿继续编辑,或打开服务器版本;草稿会保留到下次保存确认。 '
  const recover = $('#doc-draft-recover')
  recover.hidden = false
  recover.onclick = () => {
    overlay.hidden = true
    $('#composer').hidden = false
    void startEditor(draft)
  }
  const openServer = $('#doc-server-open')
  openServer.hidden = false
  openServer.onclick = () => {
    overlay.hidden = true
    $('#composer').hidden = false
    void startEditor(doc)
  }
  overlay.hidden = false
  $('#composer').hidden = true
  void finishCanvasBoot()
}

/** ?stress=N 压测模式(F1):合成文档不落盘,N 个对象 + FPS 计;详见 stress.ts */
const stressCount = Math.max(
  0,
  Math.floor(Number(new URLSearchParams(location.search).get('stress')) || 0),
)

async function bootDocument() {
  if (stressCount > 0) {
    await startEditor({ id: 'stress', name: '压力测试', objects: {}, order: [], revision: 1 })
    return
  }
  try {
    const doc = await openDocument()
    const draft = loadLocalDraft(doc.id)
    const resolution = resolveDraftOnOpen(doc, draft)
    if (resolution.action === 'needs-choice') {
      // 未确认草稿(保存失败/冲突/beacon 未送达):不静默清除,由用户显式选择(已知问题 #2)
      showDraftChoice(doc, resolution.draft)
      return
    }
    if (resolution.clearDraft) {
      // 服务端已包含草稿内容(等价保存确认),清掉兜底
      clearLocalDraft(doc.id)
    }
    await startEditor(doc)
  } catch (err) {
    if (err instanceof DocumentMissingError) {
      const docId = new URLSearchParams(location.search).get('doc') || ''
      if (docId) {
        clearLocalDraft(docId)
      }
      showDocProblem('文档不存在', '链接指向的画布已被删除或从未创建。 ')
      return
    }
    // 服务端读取失败(PRD §9/A13):有本机草稿时提供恢复入口
    const docId =
      new URLSearchParams(location.search).get('doc') ||
      localStorage.getItem('gencanvas.docId') ||
      ''
    const draft = docId ? loadLocalDraft(docId) : null
    if (draft && draft.id === docId) {
      const overlay = $('#doc-missing')
      $('#doc-missing-title').textContent = '画布读取失败'
      $('#doc-missing-hint').textContent =
        '服务暂不可用。已找到本机草稿,可先恢复继续编辑;保存恢复后会再与服务端同步。 '
      const recover = $('#doc-draft-recover')
      recover.hidden = false
      recover.onclick = () => {
        overlay.hidden = true
        $('#composer').hidden = false
        void startEditor(draft)
      }
      overlay.hidden = false
      $('#composer').hidden = true
    } else {
      showDocProblem('画布读取失败', err instanceof Error ? err.message : String(err))
    }
  }
}

/** 编辑器启动主体:文档来源(服务端/本机草稿/压测)确定后走到这里。 */
async function startEditor(doc: CanvasDocData) {
  docNameInput.value = doc.name || '未命名画布'
  migrateNodes(doc.objects)
  const store = createDocStore(doc)
  const saver =
    stressCount > 0
      ? null
      : createAutoSaver({
          docId: doc.id,
          getDoc: () => store.doc,
          onStatus: updateSaveStatus,
        })
  autoSaver = saver

  // 对话栏(先于 cutout:抠图结果要写进对话)
  /** 结果待确认查询后手动触发对账 */
  let genEvents: GenerationEventsHandle | null = null
  chat = createChat({
    docStore: store,
    container: $('#chat-messages'),
    actions: {
      focusObject: (objId) => focusObject(objId),
      openMask: (objId, maskUrl, sessionId) => {
        const obj = store.doc.objects[objId]
        if (obj) {
          void cutoutSession?.open(obj, { maskUrl, sessionId })
        }
      },
      regenerateGroup: (groupId) => {
        toast('已提交新的一组生成(将产生新的生成调用)')
        void regenerateGroup({ docStore: store, groupId, chat: chat!, engine })
      },
      confirmGroup: (groupId) => {
        void confirmPendingGroup({ docStore: store, groupId, chat: chat! }).then((result) => {
          if (result.found) {
            // 服务端确实已接受:交还给对账/事件流接管(A06)
            toast('已找到该次请求,继续生成流程')
            void genEvents?.reconcileNow()
          }
        })
      },
      placeResult: (info) => {
        placeCutoutResult(store, info)
      },
    },
  })

  // 对象层:渲染 + 选择/拖动/删除
  const objects = createObjectLayer({
    engine,
    getDoc: () => store.doc,
    applyCommand: (cmd) => store.apply(cmd),
    onSelectionChange: (ids) => {
      selection = ids
      nodePanel?.refresh()
      // 选择变化时允许"轻移视口"给操作栏腾出空间(PRD §4.1)
      queueFrame(() => objectActions?.refresh({ pan: true }))
    },
    onCancelPlaceholder: (objId) => {
      void cancelPlaceholder({ docStore: store, objId })
    },
    onCancelTransient: (objId) => {
      void cancelPlaceholder({ docStore: store, objId })
    },
    onRetryError: (objId) => {
      void import('./flows/generate.js').then((m) =>
        m.retryError({
          docStore: store,
          objId,
          resolveReference: async (fromId) => {
            const ref = store.doc.objects[fromId]
            if (!ref || (ref.kind !== 'image' && ref.kind !== 'video')) {
              return null
            }
            return { dataUrl: await objectToDataUrl(ref) }
          },
        }),
      )
    },
    imageUrlsFor: (obj) => {
      if (!obj.assetId) {
        return { original: obj.src || '' }
      }
      const base = `/assets/${obj.assetId}`
      return {
        thumb256: obj.hasThumbs === false ? null : `${base}/t256.webp`,
        thumb1024: obj.hasThumbs === false ? null : `${base}/t1024.webp`,
        original: `${base}/original.${obj.ext || 'png'}`,
      }
    },
  })
  objectLayer = objects
  objects.sync() // 首次渲染:订阅只在变更时触发,纯加载(无变更)也需要建组
  store.subscribe(({ doc: current }) => {
    const activeTarget = engine.interaction.getState().targetId
    if (activeTarget && !current.objects[activeTarget]) {
      cutoutSession?.exit()
      inpaint?.exit()
      setInpaintUi(false)
    }
    objects.sync()
    queueFrame(() => objectActions?.refresh())
    saver?.markDirty()
    canvasEmpty.hidden = current.order.length > 0
    updateJobCount(current)
    fitInitialView()
  })
  canvasEmpty.hidden = doc.order.length > 0
  // 先挂 docStore 再取初始视野:fitInitialView 需要读到当前文档;
  // 舞台尺寸未就绪时会在此失败,由 onSizeChanged 重放
  docStore = store
  fitInitialView()
  if (saver) {
    window.addEventListener('beforeunload', () => saver.flushOnUnload())
  }

  inpaint = createInpaintSession({
    engine,
    getDocStore: () => store,
    onSubmit: async ({ srcObj, mask, prompt }) => {
      const genParams: Record<string, unknown> = {
        prompt,
        negativePrompt: '',
        model: defaultModel,
        // 用蒙版的自然尺寸(源图原始像素)而非画布显示尺寸,避免蒙版/尺寸错位
        width: mask.width,
        height: mask.height,
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
      const initImage = { dataUrl: await objectToDataUrl(srcObj) }
      await submitGeneration({
        docStore: store,
        anchor: { x: srcObj.x + srcObj.width + 40, y: srcObj.y },
        genParams,
        initImage,
        maskImage: { dataUrl: mask.dataUrl },
        lineage: { fromId: srcObj.id, params: { ...genParams } },
      })
    },
  })
  cutoutSession = createCutoutSession({
    getDocStore: () => store,
    engine,
    getSelection: () => selection,
    pickObject: (world) => objectLayer?.pickObjectAt(world),
    onSelectObject: (id) => objectLayer?.setSelected([id]),
    toast,
    // 整理位置建议(PRD §6):派生结果与其他组相交时给出提示,不自动搬动
    onSuggest: (text) => chat?.addSystem(text),
    onResult: ({
      objId,
      assetUrl,
      sourceLabel,
      sessionId,
      failed,
      missing,
      assetId,
      ext,
      width,
      height,
      hasThumbs,
    }) => {
      if (failed) {
        chat?.addError('抠图失败:原蒙版草稿已保留,可修正后再次提交')
        return
      }
      if (missing) {
        // 资产已生成但占位被删:保留结果,由用户显式放入(PRD §6)
        chat?.addResultCard({
          assetUrl,
          sourceLabel: sourceLabel || '画布源图',
          sessionId,
          pendingPlace: true,
          assetId,
          ext,
          width,
          height,
          hasThumbs,
        })
        return
      }
      if (sessionId && agentFlow) {
        agentFlow.markCompleted(sessionId, assetUrl, objId)
      } else {
        chat?.addResultCard({ objId, assetUrl, sourceLabel: sourceLabel || '画布源图' })
      }
    },
  })
  agentFlow = createAgentFlow({
    docStore: store,
    chat,
  })
  contextMenu.attach(engine, () => objectLayer!)

  // 右键菜单「生成视频」:图像对象 → 参数对话框
  document.querySelector('#ctx-video')?.addEventListener('click', () => {
    const sel = objectLayer?.getSelected() ?? []
    const obj = sel.length > 0 ? store.doc.objects[sel[0]] : null
    if (obj && obj.kind === 'image') {
      openVideoDialog(obj.id)
    }
  })
  const createMenu = initCreateMenu({
    engine,
    getStore: () => docStore,
    onCreated: (id) => {
      objectLayer?.setSelected([id])
      nodePanel?.refresh()
    },
  })
  // 双击分发:视频 → 播放;图像 → 局部重绘;文字 → 编辑;画板 → 重命名
  engine.stage.container().addEventListener('dblclick', (e: MouseEvent) => {
    if (!docStore) {
      return
    }
    const rect = engine.stage.container().getBoundingClientRect()
    const pos = { x: e.clientX - rect.left, y: e.clientY - rect.top }
    const hit = engine.contentLayer.getIntersection(pos)
    if (!hit) {
      createMenu.open(e.clientX, e.clientY, engine.screenToWorld(pos))
      return
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Konva 以 UMD vendor 引入,无类型来源
    let node: any = hit
    while (node && !docStore.doc.objects[node.id()]) {
      node = node.parent
    }
    const obj = node ? docStore.doc.objects[node.id()] : null
    if (!obj) {
      return
    }
    if (obj.kind === 'video' && (obj.src || obj.assetId)) {
      playVideo(obj)
      return
    }
    if (obj.kind === 'text') {
      startTextEdit(obj.id)
      return
    }
    if (obj.kind === 'board') {
      renameBoard(obj.id)
      return
    }
  })
  // 右键菜单「局部重绘」:仅图像对象可用
  document.querySelector('#ctx-inpaint')?.addEventListener('click', () => {
    const sel = objectLayer?.getSelected() ?? []
    const obj = sel.length > 0 ? store.doc.objects[sel[0]] : null
    if (obj && obj.kind === 'image' && inpaint?.enter(obj.id)) {
      setInpaintUi(true)
    }
  })
  // 素材库抽屉:历史与已上传资产,拖入画布或点击加入(导入资产)
  const library = initLibrary({
    getDocStore: () => store,
    onAddToCanvas: (item, world) => {
      if (!docStore || !engine.interaction.canEdit()) {
        return
      }
      const cam = engine.getCam()
      const anchor = world ?? {
        x: cam.x + engine.stage.width() / cam.scale / 2 - item.width / 2,
        y: cam.y + engine.stage.height() / cam.scale / 2 - item.height / 2,
      }
      store.apply({
        type: 'addObjects',
        objects: [
          {
            id: `lib-${Date.now().toString(36)}-${Math.floor(Math.random() * 1e4)}`,
            kind: item.kind,
            x: anchor.x,
            y: anchor.y,
            width: item.width,
            height: item.height,
            src: item.url,
            imageId: item.id,
          },
        ],
      })
    },
  })
  library.attachDropTarget(engine)
  libraryDrawer = library
  // 顶部「素材库」按钮的开关逻辑由 library.ts 自管(避免重复绑定导致抽屉关不上)

  // 选中对象操作栏
  objectActions = initObjectActions({
    engine,
    rootEl,
    getDocStore: () => store,
    getSelection: () => selection,
    onAddToChat: (objId) => addToConversation(objId),
    onCutout: (objId) => {
      const obj = store.doc.objects[objId]
      if (obj && obj.kind === 'image') {
        void cutoutSession?.open(obj)
      }
    },
    toast,
  })

  nodePanel = createNodePanel({
    store,
    engine,
    getSelection: () => selection,
    select: (id) => objects.setSelected([id]),
    pickObject: (point) => objects.pickObjectAt(point),
    editText: startTextEdit,
    notify: toast,
  })

  // SSE 生成事件驱动占位状态机:进度/整组发布/失败/对账
  genEvents = connectGenerationEvents({
    docStore: store,
    onConnectionChange: (connected) => {
      const banner = $('#offline-banner')
      banner.hidden = connected
    },
    onReconciled: ({ changed }) => {
      if (changed > 0) {
        objects.sync()
      }
    },
  })

  // 刷新后恢复 Agent 会话(等待蒙版/已完成),不自动重提(PRD A11)
  if (stressCount === 0) {
    void agentFlow.recover()
  }

  if (stressCount > 0) {
    const { installStress } = await import('./stress.js')
    await installStress(stressCount, { store })
    fitToContent(store.doc)
  }

  // 输入框初始落位:对话栏可见时置于对话栏底部(PRD 约束 3 修订)
  syncComposerLocation()
  await finishCanvasBoot(store.doc)
}

function updateJobCount(doc: Pick<CanvasDocData, 'objects'>) {
  const active = Object.values(doc.objects).filter(
    (o) => o.kind === 'placeholder' && o.gen?.jobId && !String(o.gen.jobId).startsWith('local-'),
  ).length
  const el = $('#job-count-value')
  if (el) {
    el.textContent = String(active)
  }
}

/** 定位并选中对象(对话卡"查看/定位"入口)。 */
function focusObject(objId: string) {
  if (!docStore?.doc.objects[objId]) {
    return
  }
  objectLayer?.setSelected([objId])
  objectLayer?.revealObject(objId)
  setChatOpen(true)
}

/* ---------- 对话栏折叠(窄窗主动收起,PRD §4.1) ---------- */

$('#chat-collapse').addEventListener('click', () => setChatOpen(false))
$('#chat-expand').addEventListener('click', () => setChatOpen(true))

/** 对话栏开合(宽/窄窗统一语义):开 = 移除折叠并在窄窗挂起浮层;关 = 折叠。 */
function setChatOpen(open: boolean) {
  const body = $('#editor-body')
  body.classList.toggle('chat-collapsed', !open)
  body.classList.toggle('chat-open-narrow', open && narrowChatQuery.matches)
  $('#chat-expand').hidden = open
  syncComposerLocation()
}

/* 输入区始终属于悬浮对话面板，收起时保留草稿。 */

/** @returns 对话栏当前是否可见 */
function isChatVisible() {
  const body = $('#editor-body')
  if (body.classList.contains('chat-collapsed')) {
    return false
  }
  if (window.matchMedia('(max-width: 1023px)').matches) {
    return body.classList.contains('chat-open-narrow')
  }
  return true
}

/** 同步悬浮面板与入口，输入草稿不随开合重置。 */
function syncComposerLocation() {
  const composer = $('#composer')
  const chatVisible = isChatVisible()
  $('#chat-expand').hidden = chatVisible
  $('#chat-expand').setAttribute('aria-expanded', String(chatVisible))
  const panel = $('#chat-panel')
  panel.inert = !chatVisible
  panel.setAttribute('aria-hidden', String(!chatVisible))
  if (!chatVisible && panel.contains(document.activeElement)) {
    $('#chat-expand').focus()
  }
  if (composer.parentElement !== panel) {
    panel.append(composer)
  }
  composer.hidden = inpaintBarActive || maskBarActive
  syncHeaderPosition()
}

/** 让右上工具组跟随对话面板，弹出层使用同一偏移。 */
function syncHeaderPosition() {
  const width = $('#chat-panel').offsetWidth
  const offset = isChatVisible() && window.innerWidth >= 1024 ? width + 12 : 0
  document.documentElement.style.setProperty('--chat-header-offset', `${offset}px`)
}

const narrowChatQuery = window.matchMedia('(max-width: 1023px)')
narrowChatQuery.addEventListener('change', () => syncComposerLocation())
window.addEventListener('resize', () => syncComposerLocation())
// 低频兜底:个别环境(如嵌入式视口)不派发 resize/媒体查询事件,靠轮询纠正落位
let lastLocationCheck = { w: window.innerWidth, visible: isChatVisible() }
setInterval(() => {
  const w = window.innerWidth
  const visible = isChatVisible()
  if (w !== lastLocationCheck.w || visible !== lastLocationCheck.visible) {
    lastLocationCheck = { w, visible }
    syncComposerLocation()
  }
}, 1000)

/* ---------- 悬浮对话面板调宽:鼠标拖拽与键盘，记住本机偏好 ---------- */
const CHAT_WIDTH_KEY = 'gencanvas.chatWidth'
function initChatResize() {
  const panel = $('#chat-panel')
  const grip = document.createElement('div')
  grip.className = 'chat-resizer'
  grip.title = '拖拽调整对话栏宽度'
  grip.tabIndex = 0
  grip.setAttribute('role', 'separator')
  grip.setAttribute('aria-label', '调整对话面板宽度')
  grip.setAttribute('aria-orientation', 'vertical')
  grip.setAttribute('aria-controls', 'chat-panel')
  function resize(width: number) {
    const max = Math.max(240, Math.min(600, window.innerWidth - 80))
    const min = Math.min(320, max)
    const next = Math.round(Math.min(max, Math.max(min, width)))
    panel.style.width = `${next}px`
    syncHeaderPosition()
    grip.setAttribute('aria-valuemin', String(min))
    grip.setAttribute('aria-valuemax', String(max))
    grip.setAttribute('aria-valuenow', String(next))
  }
  function saveWidth() {
    try {
      localStorage.setItem(CHAT_WIDTH_KEY, panel.style.width.replace('px', ''))
    } catch {
      /* 本机偏好不可用时，保留当前会话宽度。 */
    }
  }
  let initial = 400
  try {
    const saved = Number(localStorage.getItem(CHAT_WIDTH_KEY))
    if (Number.isFinite(saved) && saved >= 320 && saved <= 600) {
      initial = saved
    }
  } catch {
    /* 使用默认宽度。 */
  }
  resize(initial)
  window.addEventListener('resize', () => resize(parseFloat(panel.style.width)))
  grip.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') {
      return
    }
    event.preventDefault()
    resize(panel.getBoundingClientRect().width + (event.key === 'ArrowLeft' ? 20 : -20))
    saveWidth()
  })
  grip.addEventListener('pointerdown', (event) => {
    event.preventDefault()
    grip.setPointerCapture(event.pointerId)
    const right = panel.getBoundingClientRect().right
    const move = (next: PointerEvent) => resize(right - next.clientX)
    const end = () => {
      grip.removeEventListener('pointermove', move)
      grip.removeEventListener('pointerup', end)
      grip.removeEventListener('pointercancel', end)
      saveWidth()
    }
    grip.addEventListener('pointermove', move)
    grip.addEventListener('pointerup', end)
    grip.addEventListener('pointercancel', end)
  })
  panel.append(grip)
}
initChatResize()

/* ---------- 视野框住全部对象(首次加载) ---------- */

/** 视野框住全部内容;舞台尚未拿到布局尺寸(0×0)时返回 false,由 onSizeChanged 重放 */
function fitToContent(doc: Pick<CanvasDocData, 'objects'>): boolean {
  const list = Object.values(doc.objects)
  if (list.length === 0 || engine.stage.width() <= 0 || engine.stage.height() <= 0) {
    return false
  }
  const minX = Math.min(...list.map((o) => o.x))
  const minY = Math.min(...list.map((o) => o.y))
  const maxX = Math.max(...list.map((o) => o.x + o.width))
  const maxY = Math.max(...list.map((o) => o.y + o.height))
  const margin = 80
  const scale = Math.min(
    1,
    (engine.stage.width() - margin * 2) / (maxX - minX),
    (engine.stage.height() - margin * 2) / (maxY - minY),
  )
  engine.centerOn({ x: (minX + maxX) / 2, y: (minY + maxY) / 2 }, Math.max(0.05, scale))
  return true
}

queueMicrotask(() => {
  void bootDocument()
})

void initSettings({
  // 后端热切换后默认模型可能变化:刷新默认模型
  onConfigChanged: () => {
    void loadDefaultModel()
  },
})

/* ---------- 默认模型(/api/models 首个;云端只有一个可用模型) ---------- */

let defaultModel = ''
async function loadDefaultModel() {
  try {
    const res = await fetch('/api/models')
    const body = (await res.json()) as { models?: { id: string }[] }
    defaultModel = body.models?.[0]?.id || ''
  } catch {
    defaultModel = ''
  }
}
void loadDefaultModel()

/* ---------- 生成能力(参考程度与视频模式均按能力门控;不支持时隐藏/禁用并不上送) ---------- */

let videoCapability: Capability | null = null

async function loadGenerationCapabilities() {
  try {
    const res = await fetch('/api/generation-capabilities')
    const body = (await res.json()) as {
      image?: { referenceWeightAdjustable?: boolean }
      video?: Capability
    }
    weightAdjustable = Boolean(body.image?.referenceWeightAdjustable)
    videoCapability = body.video ?? null
  } catch {
    weightAdjustable = false
    videoCapability = null
  }
  applyComposerCapabilities()
  refreshComposerReferenceUI()
}

/** 视频画幅/时长下拉随能力刷新;能力不可用时禁用「生成视频」模式项并给出原因 */
function applyComposerCapabilities() {
  const videoSelect = $('#composer-video-size') as HTMLSelectElement
  const durationSelect = $('#composer-duration') as HTMLSelectElement
  const modeOption = document.querySelector(
    '#composer-mode option[value="video"]',
  ) as HTMLOptionElement | null
  const video = videoCapability
  if (!video) {
    videoSelect.replaceChildren()
    durationSelect.replaceChildren()
    if (modeOption) {
      modeOption.disabled = true
      modeOption.textContent = '生成视频(能力不可用)'
      modeOption.title = '无法读取生成能力,请检查服务'
    }
    return
  }
  fillSelect(videoSelect, videoFormatOptions(video))
  fillSelect(durationSelect, videoDurationOptions(video))
  if (modeOption) {
    modeOption.disabled = !video.supported
    modeOption.textContent = video.supported ? '生成视频' : '生成视频(当前后端不可用)'
    modeOption.title = video.supported ? '' : video.reason || '当前后端不支持视频生成'
  }
}
void loadGenerationCapabilities()

/* ---------- 断线「立即重试」+ 多标签页检测 ---------- */

$('#offline-retry').addEventListener('click', () => {
  location.reload()
})

watchDuplicateTabs({
  onDuplicate: () => {
    const banner = $('#offline-banner')
    banner.hidden = false
    const label = banner.querySelector('span')
    if (label) {
      label.textContent = '检测到多个画布标签页,文档可能互相覆盖,建议只保留一个'
    }
    setTimeout(() => {
      banner.hidden = true
      if (label) {
        label.textContent = '连接断开,正在重连…'
      }
    }, 8000)
  },
})

/* ---------- 键盘:撤销重做 / 删除 / Tab 聚焦输入区 ---------- */

const isEditable = (el: EventTarget | null) =>
  el instanceof HTMLElement &&
  (el.tagName === 'INPUT' ||
    el.tagName === 'TEXTAREA' ||
    el.tagName === 'SELECT' ||
    el.isContentEditable ||
    el.closest?.('sl-input, sl-textarea, [contenteditable]') != null)

engine.input.setSelectionKey((e: KeyboardEvent) => {
  if (nodePanel?.handleReferenceKey(e)) {
    return
  }
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'z' && !isEditable(e.target)) {
    e.preventDefault()
    if (e.shiftKey) {
      docStore?.redo()
    } else {
      docStore?.undo()
    }
    return
  }
  if (
    (e.key === 'Delete' || e.key === 'Backspace') &&
    !isEditable(e.target) &&
    selection.length > 0
  ) {
    e.preventDefault()
    objectLayer?.deleteSelected()
    return
  }
  if (e.key === 'Escape' && !isEditable(e.target)) {
    objectLayer?.setSelected([])
  }
  if (e.key === 'Tab' && !isEditable(e.target)) {
    e.preventDefault()
    composerInput.focus()
  }
})

/* ---------- 底部输入区:生成图片 / 创作助手(PRD §5.1 / §7) ---------- */

let composerMode: ComposerMode = 'generate'
/** 输入卡显式引用的图片对象 id(生成图片与创作助手共用,不随选择偷偷替换) */
let composerRefId: string | null = null
/** 当前能力是否支持调节参考程度(仅 ComfyUI/Mock;云端为 false 时隐藏控件,不上送该字段) */
let weightAdjustable = false

const submitBtn = $('#composer-submit') as HTMLButtonElement
const composerHint = $('#composer-hint')

/** 引用与参考程度控件的可见性(引用在图片/助手模式可见;视频模式为文生视频,不挂参考图) */
function refreshComposerReferenceUI() {
  $('#composer-ref').hidden = !composerRefId || composerMode === 'video'
  const weightSelect = $('#composer-weight') as HTMLSelectElement
  weightSelect.hidden = !(composerMode === 'generate' && composerRefId && weightAdjustable)
  if (composerMode === 'generate') {
    composerHint.textContent = composerRefId
      ? '图生图 · 4 个方向 × 每方向 1 张'
      : '4 个方向 × 每方向 1 张'
  } else if (composerMode === 'video') {
    composerHint.textContent = '文生视频 · 单条付费任务'
  }
}

function setComposerMode(mode: ComposerMode) {
  composerMode = mode
  const modeSelect = document.querySelector('#composer-mode') as HTMLSelectElement
  modeSelect.value = mode
  const video = mode === 'video'
  $('#composer-size').hidden = mode !== 'generate'
  const videoSize = $('#composer-video-size') as HTMLSelectElement
  const videoDuration = $('#composer-duration') as HTMLSelectElement
  videoSize.hidden = !video
  videoDuration.hidden = !video
  composerInput.placeholder =
    mode === 'generate'
      ? composerRefId
        ? '描述如何基于这张参考图创作，例如：换成雪山背景，保持人物不变…'
        : '描述你想创作的画面，或将画布图片添加到对话…'
      : mode === 'video'
        ? '描述你想生成的视频画面，例如：海浪拍打礁石，日落延时摄影…'
        : '告诉助手你想如何修改，例如：保留主体，换一个背景…'
  composerHint.textContent =
    mode === 'generate'
      ? '4 个方向 × 每方向 1 张'
      : video
        ? '文生视频 · 单条付费任务'
        : '助手将先给出计划,蒙版需你确认'
  submitBtn.textContent = mode === 'generate' ? '生成 4 张' : video ? '生成视频' : '发送'
  refreshComposerReferenceUI()
}
const composerMenus = Array.from(
  document.querySelectorAll('.composer-settings'),
) as HTMLDetailsElement[]
function closeComposerMenus() {
  for (const menu of composerMenus) {
    menu.open = false
  }
}
document
  .querySelector('#composer-mentions > summary')
  ?.addEventListener('click', renderComposerReferences)

function renderComposerReferences() {
  const list = $('#composer-reference-list')
  list.replaceChildren()
  const heading = document.createElement('span')
  heading.className = 'composer-menu-label'
  heading.textContent = '引用给 Agent 的画布图片'
  list.append(heading)
  const images = Object.values(docStore?.doc.objects || {}).filter((obj) => obj.kind === 'image')
  if (!images.length) {
    const empty = document.createElement('p')
    empty.textContent = '画布还没有图片，请先上传或添加资产。'
    list.append(empty)
  }
  images.forEach((obj, index) => {
    const button = document.createElement('button')
    button.type = 'button'
    button.textContent = obj.directionTitle || `画布图片 ${index + 1}`
    button.addEventListener('click', () => {
      closeComposerMenus()
      addToConversation(obj.id)
    })
    list.append(button)
  })
}
$('#composer-upload').addEventListener('click', () => {
  closeComposerMenus()
  $('#tool-upload').click()
})
$('#composer-assets').addEventListener('click', () => {
  closeComposerMenus()
  libraryDrawer?.open()
})
$('#composer-from-canvas').addEventListener('click', () => {
  closeComposerMenus()
  renderComposerReferences()
  const mentions = $('#composer-mentions') as HTMLDetailsElement
  mentions.open = true
})
document.querySelectorAll('[data-composer-skill]').forEach((button) => {
  button.addEventListener('click', () => {
    const skill = button.getAttribute('data-composer-skill')
    setComposerMode(skill === 'cutout' ? 'agent' : 'generate')
    const current = composerInput.value.trim()
    if (skill === 'cutout') {
      composerInput.value = current || '请抠出主体，移除背景，保留透明背景。'
    } else if (skill === 'poster') {
      composerInput.value = current
        ? `请创作一张海报，注重主体、构图与文字层级。主题：${current}`
        : '请创作一张海报，注重主体、构图与文字层级。主题：'
    }
    submitBtn.disabled = !composerInput.value.trim()
    closeComposerMenus()
    composerInput.focus()
    if (skill === 'cutout' && !composerRefId) {
      renderComposerReferences()
      const mentions = $('#composer-mentions') as HTMLDetailsElement
      mentions.open = true
    }
  })
})

$('#composer-mode').addEventListener('change', (e: Event) => {
  const value = (e.target as HTMLSelectElement).value
  const mode: ComposerMode = value === 'agent' || value === 'video' ? value : 'generate'
  setComposerMode(mode)
})

/** 添加到对话:显式引用(缩略图 + 名称,可移除;不随选择偷偷替换)。 */
function addToConversation(objId: string) {
  const obj = docStore?.doc.objects[objId]
  if (!obj || obj.kind !== 'image') {
    return
  }
  composerRefId = objId
  const thumb = $('#composer-ref-thumb') as HTMLImageElement
  thumb.src = obj.assetId
    ? `/assets/${obj.assetId}/${obj.hasThumbs === false ? `original.${obj.ext || 'png'}` : 't256.webp'}`
    : obj.src || ''
  $('#composer-ref-name').textContent = obj.directionTitle || '画布图片'
  // 引用在生成图片与创作助手两种模式共用:生成模式即图生图,助手模式即抠图引用,不强制切换模式。
  refreshComposerReferenceUI()
  composerInput.focus()
  toast('已添加到对话')
}
$('#composer-ref-remove').addEventListener('click', () => {
  composerRefId = null
  refreshComposerReferenceUI()
})

composerInput.addEventListener('input', () => {
  submitBtn.disabled = composerInput.value.trim().length === 0
})
composerInput.addEventListener('keydown', (e: KeyboardEvent) => {
  // Cmd/Ctrl+Enter 提交;Enter 换行
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
    e.preventDefault()
    if (!submitBtn.disabled) {
      submitBtn.click()
    }
  }
})

submitBtn.addEventListener('click', async () => {
  if (!docStore) {
    return
  }
  const text = composerInput.value.trim()
  if (!text) {
    return
  }
  submitBtn.disabled = true
  try {
    if (composerMode === 'generate') {
      const [w, h] = String(($('#composer-size') as HTMLSelectElement).value)
        .split('x')
        .map(Number)
      const genParams: Record<string, unknown> = {
        prompt: text,
        model: defaultModel,
        width: w || 1024,
        height: h || 1024,
        kind: 'image',
      }
      if (composerRefId) {
        // 图生图(技术方案 v0.1 §4):引用图先解析为服务端资产;强度仅能力支持时上送。
        try {
          const reference = await resolveComposerReference(docStore.doc.objects[composerRefId])
          genParams.referenceAssetIds = [reference.assetId]
          const weight = readReferenceWeight(
            document.querySelector<HTMLSelectElement>('#composer-weight'),
          )
          if (weight && weightAdjustable) {
            genParams.referenceWeight = weight
          }
        } catch (err) {
          toast(err instanceof Error ? err.message : String(err))
          return
        }
      }
      const result = await submitGroupGeneration({ docStore, engine, genParams, chat: chat! })
      if (result.ok) {
        composerInput.value = ''
      }
    } else if (composerMode === 'video') {
      // 文生视频(技术方案 v0.1 §5.1):单条任务,无源图,不伪造图生视频(PRD §8)。
      if (videoCapability && !videoCapability.supported) {
        toast(videoCapability.reason || '当前后端不支持视频生成')
        setComposerMode('generate')
        return
      }
      const [w, h] = String(($('#composer-video-size') as HTMLSelectElement).value || '512x512')
        .split('x')
        .map(Number)
      const durationSec = Number(($('#composer-duration') as HTMLSelectElement).value) || 4
      const genParams: Record<string, unknown> = {
        prompt: text,
        model: defaultModel,
        width: w || 512,
        height: h || 512,
        kind: 'video',
        durationSec,
        fps: 16,
        steps: 20,
        cfgScale: 7,
        seed: -1,
        denoise: 1,
      }
      const cam = engine.getCam()
      const result = await submitGeneration({
        docStore,
        anchor: {
          x: cam.x + engine.stage.width() / cam.scale / 2,
          y: cam.y + engine.stage.height() / cam.scale / 2,
        },
        genParams,
      })
      if (result.ok) {
        composerInput.value = ''
      }
    } else {
      const result = await agentFlow!.send({ request: text, refObjId: composerRefId })
      if (result.ok) {
        composerInput.value = ''
      }
    }
  } finally {
    submitBtn.disabled = composerInput.value.trim().length === 0
  }
})

/* ---------- 后端可用性:服务不可用时禁用提交并说明(PRD §5.2) ---------- */

async function checkBackend() {
  try {
    const res = await fetch('/api/health')
    const body = (await res.json()) as { ok?: boolean; backend?: { ok?: boolean; detail?: string } }
    const ok = Boolean(body.ok && body.backend?.ok !== false)
    composerHint.classList.toggle('off', !ok)
    if (!ok) {
      composerHint.textContent = `生成服务不可用:${body.backend?.detail || '请检查设置'}`
      submitBtn.disabled = true
      submitBtn.title = composerHint.textContent
    } else if (composerMode === 'generate') {
      composerHint.textContent = '4 个方向 × 每方向 1 张'
      submitBtn.title = ''
      submitBtn.disabled = composerInput.value.trim().length === 0
    } else if (composerMode === 'video') {
      composerHint.textContent = '文生视频 · 单条付费任务'
      submitBtn.title = ''
      submitBtn.disabled = composerInput.value.trim().length === 0
    }
  } catch {
    // 健康检查失败不反复打断输入
  }
}
setInterval(() => void checkBackend(), 30_000)
void checkBackend()

/* ---------- 左侧工具条:本地上传 / 导入资产 / 添加画板 / 添加文字(PRD §4.2) ---------- */

/** 视口中心的世界坐标 */
function centerWorld() {
  const cam = engine.getCam()
  return {
    x: cam.x + engine.stage.width() / cam.scale / 2,
    y: cam.y + engine.stage.height() / cam.scale / 2,
  }
}

/** 当前视野对应的世界坐标矩形:新建对象落位后钳进此范围,保证用户看得见 */
function viewportWorldRect() {
  const cam = engine.getCam()
  return {
    x: cam.x,
    y: cam.y,
    width: engine.stage.width() / cam.scale,
    height: engine.stage.height() / cam.scale,
  }
}

/**
 * 把"已生成未落位"的抠图结果放入当前视口(PRD §6:结果可显式放置,不复活已删对象)。
 */
function placeCutoutResult(
  store: DocStore,
  info: {
    assetId?: string
    ext?: string
    width?: number
    height?: number
    sessionId?: string
    hasThumbs?: boolean
  },
) {
  if (!info.assetId) {
    return
  }
  const scale = Math.min(1, 640 / Math.max(info.width || 512, 1))
  const width = Math.max(64, Math.round((info.width || 512) * scale))
  const height = Math.max(64, Math.round((info.height || 512) * scale))
  const pos = clampIntoRect(
    findFreePosition(store.doc, centerWorld(), { width, height }),
    { width, height },
    viewportWorldRect(),
  )
  const objId = `cut-put-${Date.now().toString(36)}`
  store.apply(
    cmdAddObjects([
      {
        id: objId,
        kind: 'image',
        x: pos.x,
        y: pos.y,
        width,
        height,
        assetId: info.assetId,
        ext: info.ext || 'png',
        hasThumbs: info.hasThumbs !== false,
      },
    ]),
  )
  chat?.markPlaced(info.sessionId || '', objId)
  toast('已放入画布')
  focusObject(objId)
}

const uploadInput = $('#upload-input') as HTMLInputElement
$('#tool-upload').addEventListener('click', () => uploadInput.click())
uploadInput.addEventListener('change', async () => {
  const file = uploadInput.files?.[0]
  uploadInput.value = ''
  if (!file || !docStore) {
    return
  }
  if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) {
    toast('仅支持 PNG / JPEG / WebP')
    return
  }
  if (file.size > 40 * 1024 * 1024) {
    toast('文件超过 40MiB 上限')
    return
  }
  try {
    const upload = await fetch(`/api/assets?kind=image`, {
      method: 'POST',
      headers: { 'Content-Type': file.type },
      body: file,
    })
    const body = (await upload.json()) as {
      error?: string
      asset: { id: string; ext: string; width?: number; height?: number; hasThumbs?: boolean }
    }
    if (!upload.ok) {
      throw new Error(body.error || '上传失败')
    }
    const record = body.asset
    const scale = Math.min(1, 640 / Math.max(record.width || 512, 1))
    const width = Math.max(64, Math.round((record.width || 512) * scale))
    const height = Math.max(64, Math.round((record.height || 512) * scale))
    const anchor = centerWorld()
    const size = { width, height }
    const pos = clampIntoRect(
      findFreePosition(docStore.doc, anchor, size),
      size,
      viewportWorldRect(),
    )
    docStore.apply(
      cmdAddObjects([
        {
          id: `up-${Date.now().toString(36)}-${Math.floor(Math.random() * 1e4)}`,
          kind: 'image',
          x: pos.x,
          y: pos.y,
          width,
          height,
          assetId: record.id,
          ext: record.ext,
          hasThumbs: record.hasThumbs,
        },
      ]),
    )
    toast('已上传到画布')
  } catch (err) {
    // 上传失败原画布不变(PRD §4.2)
    toast(`上传失败:${err instanceof Error ? err.message : String(err)}`)
  }
})

$('#tool-board').addEventListener('click', () => {
  if (!docStore) {
    return
  }
  const boards = Object.values(docStore.doc.objects).filter((o) => o.kind === 'board').length
  const size = { width: 1024, height: 1024 }
  const pos = clampIntoRect(
    findFreePosition(docStore.doc, centerWorld(), size),
    size,
    viewportWorldRect(),
  )
  docStore.apply(
    cmdAddObjects([
      {
        id: `board-${Date.now().toString(36)}`,
        kind: 'board',
        x: pos.x,
        y: pos.y,
        width: 1024,
        height: 1024,
        name: `画板 ${boards + 1}`,
      },
    ]),
  )
})

$('#tool-text').addEventListener('click', () => {
  if (!docStore) {
    return
  }
  const size = { width: 320, height: 320 }
  const pos = clampIntoRect(
    findFreePosition(docStore.doc, centerWorld(), size),
    size,
    viewportWorldRect(),
  )
  const obj = createNode('text', pos, '文本')
  docStore.apply(cmdAddObjects([obj]))
  objectLayer?.setSelected([obj.id])
  // 新建即进入编辑:视野内可见 + 光标就位,避免"点了没反应"
  startTextEdit(obj.id)
})

/* ---------- 文字对象编辑(Esc 恢复;空文本不保留;PRD §4.2) ---------- */

const textEditor = $('#text-editor') as HTMLTextAreaElement
let textEditCancelled = false
function startTextEdit(objId: string) {
  const obj = docStore?.doc.objects[objId]
  if (!obj || obj.kind !== 'text') {
    return
  }
  editingTextId = objId
  textEditCancelled = false
  const cam = engine.getCam()
  const screen = engine.worldToScreen({ x: obj.x, y: obj.y })
  textEditor.value = obj.text || ''
  textEditor.style.left = `${screen.x}px`
  textEditor.style.top = `${screen.y}px`
  textEditor.style.width = `${Math.max(120, obj.width * cam.scale)}px`
  textEditor.style.height = `${Math.max(40, obj.height * cam.scale)}px`
  textEditor.style.fontSize = `${(obj.fontSize || 32) * cam.scale}px`
  textEditor.hidden = false
  queueMicrotask(() => textEditor.focus())
}

function commitTextEdit() {
  const objId = editingTextId
  editingTextId = null
  textEditor.hidden = true
  if (!objId || !docStore) {
    return
  }
  if (textEditCancelled) {
    return
  }
  const next = textEditor.value.replace(/\s+$/g, '')
  const obj = docStore.doc.objects[objId]
  if (!obj) {
    return
  }
  if (!next.trim() && !obj.textCard) {
    // 空文本离开时不保留空对象(PRD §4.2)
    docStore.apply(
      cmdRemoveObjects([{ object: obj, orderIndex: docStore.doc.order.indexOf(objId) }]),
    )
    return
  }
  if (next !== obj.text) {
    docStore.apply(
      cmdUpdateObject(
        objId,
        {
          text: next,
          width: obj.textCard ? obj.width : Math.max(80, next.length * (obj.fontSize || 32) * 0.62),
        },
        { text: obj.text, width: obj.width },
      ),
    )
  }
}

textEditor.addEventListener('blur', () => commitTextEdit())
textEditor.addEventListener('keydown', (e: KeyboardEvent) => {
  if (e.key === 'Escape') {
    e.preventDefault()
    textEditCancelled = true
    editingTextId = null
    textEditor.hidden = true
  }
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
    e.preventDefault()
    textEditor.blur()
  }
})

/** 画板重命名(dblclick)。 */
function renameBoard(objId: string) {
  const obj = docStore?.doc.objects[objId]
  if (!obj || obj.kind !== 'board' || !docStore) {
    return
  }
  const name = window.prompt('画板名称', obj.name || '画板')
  if (name === null) {
    return
  }
  const next = name.trim() || obj.name || '画板'
  docStore.apply(cmdUpdateObject(objId, { name: next }, { name: obj.name }))
}

/* ---------- 局部重绘 UI(保留已验证能力) ---------- */

const inpaintBar = $('#inpaint-bar')
let inpaintBarActive = false
let maskBarActive = false

/** 底部浮层互斥:输入区在重绘/蒙版栏显示期间让位(PRD §4.1 安全区) */
function syncBottomPanels() {
  $('#composer').hidden = inpaintBarActive || maskBarActive
}

// 蒙版栏由 cutout 会话内部开关,通过事件通知输入区
document.addEventListener('gencanvas:maskmode', (ev: Event) => {
  maskBarActive = Boolean((ev as CustomEvent).detail?.active)
  syncBottomPanels()
})

function setInpaintUi(active: boolean) {
  inpaintBarActive = active
  inpaintBar.hidden = !active
  syncBottomPanels()
}

document.addEventListener('gencanvas:inpaint-exit', () => setInpaintUi(false))

$('#inpaint-brush-size').addEventListener('input', (e) => {
  inpaint?.setBrush(Number((e.target as HTMLInputElement).value))
})
$('#inpaint-erase').addEventListener('click', () => {
  if (!inpaint) {
    return
  }
  const on = !inpaint.eraseOn
  inpaint.setErase(on)
  document.querySelector('#inpaint-erase')?.classList.toggle('active', on)
})
$('#inpaint-clear').addEventListener('click', () => inpaint?.clearStrokes())
$('#inpaint-exit').addEventListener('click', () => {
  inpaint?.exit()
  setInpaintUi(false)
})
$('#inpaint-generate').addEventListener('click', async () => {
  if (!inpaint || !inpaint.isActive() || !docStore) {
    return
  }
  const prompt = document.querySelector<HTMLInputElement>('#inpaint-prompt')!.value.trim()
  if (!prompt) {
    return
  }
  try {
    await inpaint.submit(prompt)
    inpaint.exit()
    setInpaintUi(false)
  } catch (err) {
    ;(document.querySelector('#inpaint-prompt') as HTMLElement).dataset.error =
      err instanceof Error ? err.message : String(err)
  }
})

/* ---------- 图生视频(保留已验证能力,PRD §8) ---------- */

/** 待生成视频的源图 id */
let videoSrcId: string | null = null

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- Shoelace 自定义元素,无类型来源
const videoDialog = $('#video-dialog') as any // Shoelace 自定义元素
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- Shoelace 自定义元素,无类型来源
const playerDialog = $('#video-player') as any
const playerBody = $('#video-player-body')

function openVideoDialog(objId: string) {
  const obj = docStore?.doc.objects[objId]
  if (!obj || obj.kind !== 'image') {
    return
  }
  videoSrcId = objId
  const thumb = $('#video-src-thumb') as HTMLImageElement
  thumb.src = obj.assetId ? `/assets/${obj.assetId}/t256.webp` : obj.src || ''
  void videoDialog.show()
}

$('#video-cancel').addEventListener('click', () => videoDialog.hide())
$('#video-generate').addEventListener('click', async () => {
  if (!docStore || !videoSrcId) {
    return
  }
  const src = docStore.doc.objects[videoSrcId]
  const durationSec = Number(($('#video-duration') as HTMLSelectElement)?.value) || 4
  const genParams: Record<string, unknown> = {
    prompt: (src.lineage?.params as { prompt?: string } | undefined)?.prompt || '让这张图动起来',
    negativePrompt: '',
    model: defaultModel,
    width: src.width,
    height: src.height,
    steps: 20,
    cfgScale: 7,
    seed: -1,
    sampler: 'euler',
    scheduler: 'normal',
    denoise: 1,
    kind: 'video',
    durationSec,
    fps: 16,
  }
  let initImage
  try {
    initImage = { dataUrl: await objectToDataUrl(src) }
  } catch {
    videoDialog.hide()
    return
  }
  videoDialog.hide()
  await submitGeneration({
    docStore,
    anchor: { x: src.x + src.width + 40, y: src.y },
    genParams,
    initImage,
    lineage: { fromId: src.id, params: { ...genParams } },
  })
})

/** 视频对象播放(带来源辨认,PRD §8:无源图即为文字描述,不伪造图生视频) */
function playVideo(obj: import('./state/commands.js').CanvasObj) {
  if (!obj) {
    return
  }
  const fromImage = Boolean(obj.lineage?.fromId)
  playerDialog.label = `视频预览 · 来源:${fromImage ? '画布图片(图生视频)' : '文字描述'}`
  const url = obj.assetId ? `/assets/${obj.assetId}/original.${obj.ext || 'mp4'}` : obj.src
  if (!url) {
    return
  }
  const isVideoFile = /\.(mp4|webm)$/i.test(url) || obj.ext === 'mp4' || obj.ext === 'webm'
  const media = document.createElement(isVideoFile ? 'video' : 'img')
  if (isVideoFile) {
    media.setAttribute('controls', '')
    media.setAttribute('autoplay', '')
    media.setAttribute('loop', '')
  }
  media.setAttribute('src', url)
  media.style.width = '100%'
  playerBody.replaceChildren(media)
  void playerDialog.show()
}

playerDialog.addEventListener('sl-after-hide', () => {
  playerBody.innerHTML = ''
})

/* ---------- 生成变体 ×4(独立 i2i 任务,保留已验证能力) ---------- */

async function generateVariants(srcObj: import('./state/commands.js').CanvasObj) {
  const store = docStore
  if (!store) {
    return
  }
  let initImage
  try {
    initImage = { dataUrl: await objectToDataUrl(srcObj) }
  } catch {
    return
  }
  const baseSeed = Math.floor(Math.random() * 2 ** 31)
  const anchors = variantRowAnchors(srcObj, 4)
  for (let i = 0; i < 4; i++) {
    const genParams: Record<string, unknown> = {
      prompt:
        (srcObj.lineage?.params as { prompt?: string } | undefined)?.prompt ||
        '基于输入图片生成一个主体和整体风格一致、细节有所变化的自然变体',
      negativePrompt: '',
      model: defaultModel,
      width: srcObj.width,
      height: srcObj.height,
      steps: 20,
      cfgScale: 7,
      seed: baseSeed + i,
      sampler: 'euler',
      scheduler: 'normal',
      denoise: 0.75,
      kind: 'image',
      durationSec: 4,
      fps: 16,
    }
    await submitGeneration({
      docStore: store,
      anchor: anchors[i],
      genParams,
      initImage,
      lineage: { fromId: srcObj.id, params: { ...genParams } },
    })
  }
}

const contextMenu: ContextMenu = initContextMenu({
  getDocStore: () => docStore,
  onOpenChange: (open) => objectActions?.setContextMenuOpen(open),
  onAddToChat: (objId) => addToConversation(objId),
  onVariants: (objId) => {
    const obj = docStore?.doc.objects[objId]
    if (obj && obj.kind === 'image') {
      void generateVariants(obj)
    }
  },
  onCutout: (objId) => {
    const obj = docStore?.doc.objects[objId]
    if (obj && obj.kind === 'image') {
      void cutoutSession?.open(obj)
    }
  },
})

// 右键菜单「下载」复用操作栏逻辑:直接触发下载
document.querySelector('#ctx-download')?.addEventListener('click', () => {
  const [objId] = selection
  const obj = objId ? docStore?.doc.objects[objId] : null
  if (obj) {
    import('./flows/asset-util.js').then(({ downloadUrl, objectOriginalUrl }) => {
      const url = objectOriginalUrl(obj)
      if (url) {
        downloadUrl(url, `gencanvas-${String(objId).slice(-6)}.${obj.ext || 'png'}`)
        toast('已开始下载')
      }
    })
  }
})

export { engine, canvasEmpty, getDocStore, getObjectLayer }

function getDocStore(): DocStore | null {
  return docStore
}
function getObjectLayer(): ObjectLayer | null {
  return objectLayer
}

// 顶部搜索与当前画布的历史对话。
let lookupMode: 'search' | 'history' = 'search'
const lookupPanel = $('#canvas-lookup')
const lookupQuery = $('#canvas-lookup-query') as HTMLInputElement
function closeLookup() {
  lookupPanel.hidden = true
  $('#canvas-search-open').setAttribute('aria-expanded', 'false')
  $('#chat-history-open').setAttribute('aria-expanded', 'false')
}
function renderLookup() {
  const results = $('#canvas-lookup-results')
  results.replaceChildren()
  const query = lookupQuery.value.trim().toLocaleLowerCase()
  const chatEntries = docStore?.doc.chat || []
  const entries =
    lookupMode === 'history'
      ? [...chatEntries].reverse().map((entry) => ({
          title:
            entry.text ||
            entry.prompt ||
            (entry.kind === 'plan' ? '助手执行计划' : '生成任务与结果'),
          detail: `${entry.role === 'user' ? '你' : '创作助手'}${entry.at ? ` · ${new Date(entry.at).toLocaleString()}` : ''}`,
          activate: () => {
            setChatOpen(true)
            const index = chatEntries.indexOf(entry)
            const message = $('#chat-messages').children[index]
            message?.scrollIntoView({ block: 'center', behavior: 'smooth' })
          },
        }))
      : Object.values(docStore?.doc.objects || {}).map((obj, index) => ({
          title: obj.directionTitle || obj.name || obj.text || `画布素材 ${index + 1}`,
          detail: [
            (
              {
                image: '图片',
                text: '文字',
                board: '画板',
                video: '视频',
                placeholder: '生成中',
                error: '生成失败',
                draft: '待创建',
              } as Record<string, string>
            )[obj.kind] || '素材',
          ]
            .filter(Boolean)
            .join(' · '),
          activate: () => focusObject(obj.id),
        }))
  const matches = entries.filter((entry) =>
    `${entry.title} ${entry.detail}`.toLocaleLowerCase().includes(query),
  )
  for (const entry of matches) {
    const button = document.createElement('button')
    button.type = 'button'
    const title = document.createElement('span')
    title.textContent = entry.title
    const detail = document.createElement('small')
    detail.textContent = entry.detail
    button.append(title, detail)
    button.addEventListener('click', () => {
      closeLookup()
      entry.activate()
    })
    results.append(button)
  }
  if (!matches.length) {
    const empty = document.createElement('p')
    empty.textContent = query
      ? '没有找到匹配内容'
      : lookupMode === 'history'
        ? '当前画布还没有对话记录'
        : '画布中还没有素材'
    results.append(empty)
  }
}
function openLookup(mode: 'search' | 'history') {
  if (!lookupPanel.hidden && lookupMode === mode) {
    closeLookup()
    return
  }
  lookupMode = mode
  lookupPanel.hidden = false
  lookupQuery.value = ''
  $('#canvas-lookup-title').textContent = mode === 'history' ? '当前画布 · 历史对话' : '搜索画布'
  lookupQuery.placeholder = mode === 'history' ? '搜索对话内容…' : '搜索画布名称…'
  $('#canvas-search-open').setAttribute('aria-expanded', String(mode === 'search'))
  $('#chat-history-open').setAttribute('aria-expanded', String(mode === 'history'))
  renderLookup()
  lookupQuery.focus()
}
$('#canvas-search-open').addEventListener('click', () => openLookup('search'))
$('#chat-history-open').addEventListener('click', () => openLookup('history'))
$('#canvas-lookup-close').addEventListener('click', closeLookup)
lookupQuery.addEventListener('input', renderLookup)
lookupPanel.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    closeLookup()
    $(lookupMode === 'history' ? '#chat-history-open' : '#canvas-search-open').focus()
  }
})
document.addEventListener('pointerdown', (event) => {
  const target = event.target as Node
  if (
    !lookupPanel.contains(target) &&
    !$('#canvas-search-open').contains(target) &&
    !$('#chat-history-open').contains(target)
  ) {
    closeLookup()
  }
})
