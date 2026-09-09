// GenCanvas 画布入口(ESM):组装引擎、文档状态层、自动保存、底部输入区、
// 右侧对话栏、四方向组生成、抠图状态机与创作助手(PRD v0.4)。 

import { createCanvasEngine } from './engine/stage.js'
import { createDocStore } from './state/doc-store.js'
import { createAutoSaver } from './state/autosave.js'
import { openDocument, renameDocument, DocumentMissingError } from './state/document-api.js'
import { createObjectLayer } from './engine/object-layer.js'
import { connectGenerationEvents } from './state/generation-events.js'
import { submitGeneration, cancelPlaceholder } from './flows/generate.js'
import { submitGroupGeneration, regenerateGroup } from './flows/group-generate.js'
import { initContextMenu } from './flows/context-menu.js'
import { createInpaintSession } from './flows/inpaint.js'
import { watchDuplicateTabs } from './state/duplicate-tabs.js'
import { initLibrary } from './flows/library.js'
import { initSettings } from './settings.js'
import { findFreePosition, variantRowAnchors } from './state/placement.js'
import { createCutoutSession } from './flows/cutout.js'
import { createChat } from './flows/chat.js'
import { createAgentFlow } from './flows/agent.js'
import { initObjectActions } from './flows/object-actions.js'
import { initThemeToggle } from './theme.js'
import { objectToDataUrl } from './flows/asset-util.js'
import { cmdAddObjects, cmdUpdateObject, cmdRemoveObjects } from './state/commands.js'

/** @param {string} sel @returns {HTMLElement} */
const $ = (sel) => /** @type {HTMLElement} */ (document.querySelector(sel))

const rootEl = $('#canvas-root')
const surfaceEl = $('#canvas-surface')
const composerInput = /** @type {HTMLTextAreaElement} */ ($('#composer-input'))
const canvasEmpty = $('#canvas-empty')
const zoomValue = $('#zoom-value')
const saveStatusEl = $('#save-status')
const docNameInput = /** @type {HTMLInputElement} */ ($('#doc-name'))

/** 轻提示(不阻塞操作,只确认"已开始/已保留"类反馈) */
/** @type {ReturnType<typeof setTimeout> | null} */
let toastTimer = null
/** @param {string} text */
function toast(text) {
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

initThemeToggle(/** @type {HTMLButtonElement} */ ($('#theme-toggle')))

/** @type {ReturnType<typeof createObjectLayer> | null} 引擎构造时会触发 onCamMove,须先声明 */
let objectLayer = null
/** @type {string[]} 当前选中(Transient) */
let selection = []
/** @type {any} 选中对象操作栏 */
let objectActions = null
/** @type {ReturnType<typeof createCutoutSession> | null} */
let cutoutSession = null
/** @type {any} 局部重绘会话 */
let inpaint = null
/** @type {any} */
let chat = null
/** @type {ReturnType<typeof initLibrary> | null} 素材库抽屉(导入资产入口) */
let libraryDrawer = null
/** @type {string | null} 正在编辑的文字对象 id */
let editingTextId = null

const engine = createCanvasEngine(rootEl, surfaceEl, {
  /** @param {{ scale: number }} cam */
  onCamMove: (cam) => {
    zoomValue.textContent = `${Math.round(cam.scale * 100)}%`
    objectLayer?.recomputeVisibility()
    if (editingTextId) {
      commitTextEdit()
    }
    queueFrame(() => objectActions?.refresh())
  },
})
// 初始视野:世界原点居中;文档加载后若已有对象会重定位
engine.centerOn({ x: 0, y: 0 }, 1)

/** rAF 合并高频刷新。 @param {() => void} fn */
function queueFrame(fn) {
  requestAnimationFrame(() => fn())
}// 缩放控件:以视口中心为锚
const viewportCenter = () => ({ x: engine.stage.width() / 2, y: engine.stage.height() / 2 })
$('#zoom-in').addEventListener('click', () => engine.zoomBy(1.2, viewportCenter()))
$('#zoom-out').addEventListener('click', () => engine.zoomBy(1 / 1.2, viewportCenter()))

/* ---------- 保存状态 + 冲突(PRD §9) ---------- */

/** @param {"idle" | "saving" | "saved" | "error" | "conflict"} status @param {string} [detail] */
function updateSaveStatus(status, detail) {
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

/** @type {ReturnType<typeof setTimeout> | null} */
let renameTimer = null
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
docNameInput.addEventListener('keydown', (/** @type {KeyboardEvent} */ e) => {
  if (e.key === 'Enter') {
    e.preventDefault()
    docNameInput.blur()
  }
})

/* ---------- 文档启动(?doc= 严格打开 / 旧入口回退) ---------- */

/** @type {ReturnType<typeof createDocStore> | null} */
let docStore = null
/** @type {ReturnType<typeof createAutoSaver> | null} */
let autoSaver = null
/** @type {any} */
let agentFlow = null
let centeredOnce = false

/** 文档不存在 / 读取失败:明确提示,不用新建兜底(PRD §3)。  */
/** @param {string} title @param {string} hint */
function showDocProblem(title, hint) {
  const overlay = $('#doc-missing')
  $('#doc-missing-title').textContent = title
  $('#doc-missing-hint').textContent = hint
  overlay.hidden = false
  $('#composer').hidden = true
}

/** ?stress=N 压测模式(F1):合成文档不落盘,N 个对象 + FPS 计;详见 stress.js */
const stressCount = Math.max(
  0,
  Math.floor(Number(new URLSearchParams(location.search).get('stress')) || 0),
)

async function bootDocument() {
  let doc
  if (stressCount > 0) {
    doc = { id: 'stress', name: '压力测试', objects: {}, order: [], revision: 1 }
  } else {
    try {
      doc = await openDocument()
    } catch (err) {
      if (err instanceof DocumentMissingError) {
        showDocProblem('文档不存在', '链接指向的画布已被删除或从未创建。 ')
      } else {
        showDocProblem('画布读取失败', err instanceof Error ? err.message : String(err))
      }
      return
    }
  }
  docNameInput.value = doc.name || '未命名画布'
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
        void regenerateGroup({ docStore: store, groupId, chat, engine })
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
      queueFrame(() => objectActions?.refresh())
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
    objects.sync()
    saver?.markDirty()
    canvasEmpty.hidden = current.order.length > 0
    updateJobCount(current)
    if (!centeredOnce && current.order.length > 0) {
      centeredOnce = true
      fitToContent(current)
    }
  })
  canvasEmpty.hidden = doc.order.length > 0
  if (doc.order.length > 0) {
    centeredOnce = true
    fitToContent(doc)
  }
  if (saver) {
    window.addEventListener('beforeunload', () => saver.flushOnUnload())
  }
  docStore = store

  inpaint = createInpaintSession({
    engine,
    getDocStore: () => store,
    onSubmit: async ({ srcObj, mask, prompt }) => {
      const genParams = {
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
    toast,
    onResult: ({ objId, assetUrl, sourceLabel, sessionId, failed }) => {
      if (failed) {
        chat?.addError('抠图失败:原蒙版草稿已保留,可修正后再次提交')
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
  contextMenu.attach(engine, () => objectLayer)

  // 右键菜单「生成视频」:图像对象 → 参数对话框
  document.querySelector('#ctx-video')?.addEventListener('click', () => {
    const sel = objectLayer?.getSelected() ?? []
    const obj = sel.length > 0 ? store.doc.objects[sel[0]] : null
    if (obj && obj.kind === 'image') {
      openVideoDialog(obj.id)
    }
  })
  // 双击分发:视频 → 播放;图像 → 局部重绘;文字 → 编辑;画板 → 重命名
  engine.stage.container().addEventListener('dblclick', (/** @type {MouseEvent} */ e) => {
    if (!docStore) {
      return
    }
    const rect = engine.stage.container().getBoundingClientRect()
    const pos = { x: e.clientX - rect.left, y: e.clientY - rect.top }
    const hit = engine.contentLayer.getIntersection(pos)
    if (!hit) {
      return
    }
    let node = hit
    while (node && !docStore.doc.objects[node.id()]) {
      node = node.parent
    }
    const obj = node ? docStore.doc.objects[node.id()] : null
    if (!obj) {
      return
    }
    if (obj.kind === 'video') {
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
    if (obj.kind === 'image' && inpaint && !inpaint.isActive() && inpaint.enter(obj.id)) {
      setInpaintUi(true)
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
    /** @param {any} item @param {{ x: number, y: number } | null} world */
    onAddToCanvas: (item, world) => {
      if (!docStore) {
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
  // 顶部「素材库」按钮的开关逻辑由 library.js 自管(避免重复绑定导致抽屉关不上)

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

  // SSE 生成事件驱动占位状态机:进度/整组发布/失败/对账
  connectGenerationEvents({
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
}

/** @param {{ objects: Record<string, any> }} doc */
function updateJobCount(doc) {
  const active = Object.values(doc.objects).filter(
    (o) => o.kind === 'placeholder' && o.gen?.jobId && !String(o.gen.jobId).startsWith('local-'),
  ).length
  const el = $('#job-count-value')
  if (el) {
    el.textContent = String(active)
  }
}

/** 定位并选中对象(对话卡"查看/定位"入口)。 @param {string} objId */
function focusObject(objId) {
  if (!docStore?.doc.objects[objId]) {
    return
  }
  objectLayer?.setSelected([objId])
  objectLayer?.revealObject(objId)
  collapseChat(false)
}

/* ---------- 对话栏折叠(窄窗主动收起,PRD §4.1) ---------- */

$('#chat-collapse').addEventListener('click', () => collapseChat(true))
$('#chat-expand').addEventListener('click', () => collapseChat(false))
/** @param {boolean} collapse */
function collapseChat(collapse) {
  $('#editor-body').classList.toggle('chat-collapsed', collapse)
  $('#editor-body').classList.remove('chat-open-narrow')
  $('#chat-expand').hidden = !collapse
}

/** 视野框住全部对象(首次加载) @param {{ objects: Record<string, any> }} doc */
function fitToContent(doc) {
  const list = Object.values(doc.objects)
  if (list.length === 0) {
    return
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
}

void bootDocument()

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
    const body = await res.json()
    defaultModel = body.models?.[0]?.id || ''
  } catch {
    defaultModel = ''
  }
}
void loadDefaultModel()

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

/** @param {EventTarget | null} el */
const isEditable = (el) =>
  el instanceof HTMLElement &&
  (el.tagName === 'INPUT' ||
    el.tagName === 'TEXTAREA' ||
    el.tagName === 'SELECT' ||
    el.isContentEditable ||
    el.closest?.('sl-input, sl-textarea, [contenteditable]') != null)

document.addEventListener('keydown', (/** @type {KeyboardEvent} */ e) => {
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

let composerMode = 'generate'
/** @type {string | null} 助手显式引用的图片对象 id */
let composerRefId = null

const submitBtn = /** @type {HTMLButtonElement} */ ($('#composer-submit'))
const composerHint = $('#composer-hint')

/** @param {'generate' | 'agent'} mode */
function setComposerMode(mode) {
  composerMode = mode
  $('#mode-generate').classList.toggle('active', mode === 'generate')
  $('#mode-agent').classList.toggle('active', mode === 'agent')
  $('#mode-generate').setAttribute('aria-selected', String(mode === 'generate'))
  $('#mode-agent').setAttribute('aria-selected', String(mode === 'agent'))
  $('#composer-ref').hidden = !(mode === 'agent' && composerRefId)
  composerHint.textContent =
    mode === 'generate' ? '4 个方向 × 每方向 1 张' : '助手将先给出计划,蒙版需你确认'
  submitBtn.textContent = mode === 'generate' ? '生成 4 张' : '发送'
}
$('#mode-generate').addEventListener('click', () => setComposerMode('generate'))
$('#mode-agent').addEventListener('click', () => setComposerMode('agent'))

/** 添加到对话:显式引用(缩略图 + 名称,可移除;不随选择偷偷替换)。 @param {string} objId */
function addToConversation(objId) {
  const obj = docStore?.doc.objects[objId]
  if (!obj || obj.kind !== 'image') {
    return
  }
  composerRefId = objId
  const thumb = /** @type {HTMLImageElement} */ ($('#composer-ref-thumb'))
  thumb.src = obj.assetId
    ? `/assets/${obj.assetId}/${obj.hasThumbs === false ? `original.${obj.ext || 'png'}` : 't256.webp'}`
    : obj.src || ''
  $('#composer-ref-name').textContent = obj.directionTitle || '画布图片'
  setComposerMode('agent')
  $('#composer-ref').hidden = false
  composerInput.focus()
  toast('已添加到对话')
}
$('#composer-ref-remove').addEventListener('click', () => {
  composerRefId = null
  $('#composer-ref').hidden = true
})

composerInput.addEventListener('input', () => {
  submitBtn.disabled = composerInput.value.trim().length === 0
})
composerInput.addEventListener('keydown', (/** @type {KeyboardEvent} */ e) => {
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
      const [w, h] = String(/** @type {HTMLSelectElement} */ ($('#composer-size')).value)
        .split('x')
        .map(Number)
      const genParams = {
        prompt: text,
        model: defaultModel,
        width: w || 1024,
        height: h || 1024,
        kind: 'image',
      }
      const result = await submitGroupGeneration({ docStore, engine, genParams, chat })
      if (result.ok) {
        composerInput.value = ''
      }
    } else {
      const result = await agentFlow.send({ request: text, refObjId: composerRefId })
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
    const body = await res.json()
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

const uploadInput = /** @type {HTMLInputElement} */ ($('#upload-input'))
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
    const body = await upload.json()
    if (!upload.ok) {
      throw new Error(body.error || '上传失败')
    }
    const record = body.asset
    const scale = Math.min(1, 640 / Math.max(record.width || 512, 1))
    const width = Math.max(64, Math.round((record.width || 512) * scale))
    const height = Math.max(64, Math.round((record.height || 512) * scale))
    const anchor = centerWorld()
    const pos = findFreePosition(docStore.doc, anchor, { width, height })
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

$('#tool-import').addEventListener('click', () => {
  // 与顶部「素材库」同一抽屉:导入已有资产复用资产本体(PRD §4.2)
  libraryDrawer?.open()
})

$('#tool-board').addEventListener('click', () => {
  if (!docStore) {
    return
  }
  const boards = Object.values(docStore.doc.objects).filter((o) => o.kind === 'board').length
  const pos = findFreePosition(docStore.doc, centerWorld(), { width: 1024, height: 1024 })
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
  const pos = findFreePosition(docStore.doc, centerWorld(), { width: 160, height: 48 })
  const id = `text-${Date.now().toString(36)}`
  docStore.apply(
    cmdAddObjects([
      {
        id,
        kind: 'text',
        x: pos.x,
        y: pos.y,
        width: 160,
        height: 48,
        text: '文字',
        fontSize: 32,
      },
    ]),
  )
  startTextEdit(id)
})

/* ---------- 文字对象编辑(Esc 恢复;空文本不保留;PRD §4.2) ---------- */

const textEditor = /** @type {HTMLTextAreaElement} */ ($('#text-editor'))
let textEditCancelled = false
/** @param {string} objId */
function startTextEdit(objId) {
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
  if (!next.trim()) {
    // 空文本离开时不保留空对象(PRD §4.2)
    docStore.apply(
      cmdRemoveObjects([
        { object: obj, orderIndex: docStore.doc.order.indexOf(objId) },
      ]),
    )
    return
  }
  if (next !== obj.text) {
    docStore.apply(
      cmdUpdateObject(objId, { text: next, width: Math.max(80, next.length * (obj.fontSize || 32) * 0.62) }, { text: obj.text, width: obj.width }),
    )
  }
}

textEditor.addEventListener('blur', () => commitTextEdit())
textEditor.addEventListener('keydown', (/** @type {KeyboardEvent} */ e) => {
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

/** 画板重命名(dblclick)。 @param {string} objId */
function renameBoard(objId) {
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
document.addEventListener('gencanvas:maskmode', (/** @type {Event} */ ev) => {
  maskBarActive = Boolean((/** @type {CustomEvent} */ (ev)).detail?.active)
  syncBottomPanels()
})

/** @param {boolean} active */
function setInpaintUi(active) {
  inpaintBarActive = active
  inpaintBar.hidden = !active
  syncBottomPanels()
}

$('#inpaint-brush-size').addEventListener('input', (e) => {
  inpaint?.setBrush(Number(/** @type {HTMLInputElement} */ (e.target).value))
})
$('#inpaint-erase').addEventListener('click', () => {
  if (!inpaint) {
    return
  }
  const on = !inpaint.eraseOn
  inpaint.setErase(on)
  /** @type {any} */ (document.querySelector('#inpaint-erase')).classList.toggle('active', on)
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
  const prompt = /** @type {HTMLInputElement} */ (document.querySelector('#inpaint-prompt')).value.trim()
  if (!prompt) {
    return
  }
  try {
    await inpaint.submit(prompt)
    inpaint.exit()
    setInpaintUi(false)
  } catch (err) {
    ;/** @type {any} */ (document.querySelector('#inpaint-prompt')).dataset.error =
      err instanceof Error ? err.message : String(err)
  }
})

/* ---------- 图生视频(保留已验证能力,PRD §8) ---------- */

/** @type {string | null} 待生成视频的源图 id */
let videoSrcId = null

const videoDialog = /** @type {any} */ ($('#video-dialog'))
const playerDialog = /** @type {any} */ ($('#video-player'))
const playerBody = $('#video-player-body')

/** @param {string} objId */
function openVideoDialog(objId) {
  const obj = docStore?.doc.objects[objId]
  if (!obj || obj.kind !== 'image') {
    return
  }
  videoSrcId = objId
  const thumb = /** @type {HTMLImageElement} */ ($('#video-src-thumb'))
  thumb.src = obj.assetId ? `/assets/${obj.assetId}/t256.webp` : obj.src || ''
  void videoDialog.show()
}

$('#video-cancel').addEventListener('click', () => videoDialog.hide())
$('#video-generate').addEventListener('click', async () => {
  if (!docStore || !videoSrcId) {
    return
  }
  const src = docStore.doc.objects[videoSrcId]
  const durationSec = Number(/** @type {any} */ ($('#video-duration'))?.value) || 4
  const genParams = {
    prompt: /** @type {any} */ (src.lineage?.params || {}).prompt || '让这张图动起来',
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

/** 视频对象播放 @param {any} obj */
function playVideo(obj) {
  if (!obj) {
    return
  }
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

/** @param {any} srcObj */
async function generateVariants(srcObj) {
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
    const genParams = {
      prompt:
        srcObj.lineage?.params?.prompt ||
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

const contextMenu = initContextMenu({
  getDocStore: () => docStore,
  onAddToChat: (/** @type {string} */ objId) => addToConversation(objId),
  onVariants: (/** @type {string} */ objId) => {
    const obj = docStore?.doc.objects[objId]
    if (obj && obj.kind === 'image') {
      void generateVariants(obj)
    }
  },
  onCutout: (/** @type {string} */ objId) => {
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

function getDocStore() {
  return docStore
}
function getObjectLayer() {
  return objectLayer
}
