// 工作台 / 我的画布(PRD §3):
//   - Tab 切换零创建;浏览任何页面都不自动新建文档(A01);
//   - 新建是幂等请求:超时保留 Idempotency-Key 重查,不产出第二份(A01);
//   - 读取失败显示错误 + 重试,不把错误伪装成空态(A02);
//   - 重命名只动名称;删除需确认。

const $ = (sel) => /** @type {HTMLElement} */ (document.querySelector(sel))

/** @param {string} path @param {RequestInit} [options] */
async function api(path, options) {
  const res = await fetch(path, options)
  const body = await res.json().catch(() => ({}))
  if (!res.ok) {
    const err = new Error(body.error || `请求失败 HTTP ${res.status}`)
    err.status = res.status
    throw err
  }
  return body
}

/** @param {string} iso */
function timeAgo(iso) {
  const ms = Date.now() - Date.parse(iso)
  if (!Number.isFinite(ms)) {
    return ''
  }
  const min = Math.floor(ms / 60000)
  if (min < 1) {return '刚刚'}
  if (min < 60) {return `${min} 分钟前`}
  const hour = Math.floor(min / 60)
  if (hour < 24) {return `${hour} 小时前`}
  const day = Math.floor(hour / 24)
  if (day < 30) {return `${day} 天前`}
  return new Date(iso).toLocaleDateString('zh-CN')
}

/* ---------- Tab 切换(?tab=;编辑器返回链接带 tab=canvas) ---------- */

const tab = new URLSearchParams(location.search).get('tab') === 'canvas' ? 'canvas' : 'overview'
function showTab(which) {
  $('#panel-overview').hidden = which !== 'overview'
  $('#panel-canvas').hidden = which !== 'canvas'
  $('#tab-overview').classList.toggle('active', which === 'overview')
  $('#tab-canvas').classList.toggle('active', which === 'canvas')
}
showTab(tab)
if (tab === 'canvas') {
  void loadDocuments()
}
void loadRecent()

/* ---------- 我的画布列表 ---------- */

/** @type {Array<{id:string,name:string,updatedAt:string,objectCount:number}>} */
let documents = []
let creating = false
/** 幂等创建 key:超时未确认时保留,重试查询同一次创建(PRD A01) */
let pendingCreateKey = null

async function loadDocuments() {
  const grid = $('#doc-grid')
  grid.innerHTML =
    '<div class="ws-skeleton-row"><span></span><span></span><span></span></div>'
  try {
    const body = await api('/api/documents')
    documents = body.documents || []
    renderDocuments()
  } catch (err) {
    // 读取失败 ≠ 空态:给出重试入口(A02)
    grid.innerHTML = ''
    const state = document.createElement('div')
    state.className = 'ws-state error'
    state.textContent = `画布列表读取失败:${err instanceof Error ? err.message : String(err)}`
    const retry = document.createElement('button')
    retry.className = 'primary-btn'
    retry.type = 'button'
    retry.textContent = '重试'
    retry.addEventListener('click', () => void loadDocuments())
    state.append(retry)
    grid.append(state)
  }
}

function renderDocuments() {
  const grid = $('#doc-grid')
  grid.innerHTML = ''
  $('#canvas-count').textContent = `共 ${documents.length} 幅`
  if (documents.length === 0) {
    const empty = document.createElement('div')
    empty.className = 'ws-state'
    empty.textContent = '还没有画布。新建一幅,一轮生成 4 个创意方向。'
    grid.append(empty)
    return
  }
  for (const doc of documents) {
    const card = document.createElement('div')
    card.className = 'doc-card'

    const name = document.createElement('div')
    name.className = 'doc-name'
    name.textContent = doc.name || '未命名画布'
    name.title = name.textContent

    const meta = document.createElement('div')
    meta.className = 'doc-meta'
    meta.textContent = `${doc.objectCount} 个对象 · ${timeAgo(doc.updatedAt)}`

    const actions = document.createElement('div')
    actions.className = 'doc-actions'

    const open = document.createElement('a')
    open.href = `/canvas?doc=${encodeURIComponent(doc.id)}`
    open.textContent = '打开'
    const rename = document.createElement('button')
    rename.type = 'button'
    rename.textContent = '重命名'
    rename.addEventListener('click', () => void renameDoc(doc))
    const del = document.createElement('button')
    del.type = 'button'
    del.className = 'danger'
    del.textContent = '删除'
    del.addEventListener('click', () => void deleteDoc(doc))

    actions.append(open, rename, del)
    card.append(name, meta, actions)
    grid.append(card)
  }
}

/** @param {{ id: string, name: string }} doc */
async function renameDoc(doc) {
  const next = window.prompt('画布名称(1–100 字符)', doc.name)
  if (next === null) {
    return
  }
  const name = next.trim().slice(0, 100) || '未命名画布'
  try {
    await api(`/api/documents/${doc.id}/rename`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    })
    void loadDocuments()
  } catch (err) {
    window.alert(`重命名失败:${err instanceof Error ? err.message : String(err)}`)
  }
}

/** @param {{ id: string, name: string }} doc */
async function deleteDoc(doc) {
  if (!window.confirm(`删除画布「${doc.name}」?已生成的素材资产会保留在素材库。`)) {
    return
  }
  try {
    await api(`/api/documents/${doc.id}`, { method: 'DELETE' })
    void loadDocuments()
    void loadRecent()
  } catch (err) {
    window.alert(`删除失败:${err instanceof Error ? err.message : String(err)}`)
  }
}

/* ---------- 幂等新建:连续点击禁用;超时保留 key 重查(不新建第二份) ---------- */

$('#create-btn').addEventListener('click', () => void createDocument())

async function createDocument() {
  if (creating) {
    return
  }
  creating = true
  const btn = /** @type {HTMLButtonElement} */ ($('#create-btn'))
  btn.disabled = true
  btn.textContent = '创建中…'
  $('#create-alert').hidden = true
  try {
    // 重试复用同一 key:服务端幂等索引保证只产出一份文档
    const key = pendingCreateKey ?? `ws-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
    pendingCreateKey = key
    const body = await api('/api/documents', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
      body: JSON.stringify({}),
    })
    pendingCreateKey = null
    location.href = `/canvas?doc=${encodeURIComponent(body.document.id)}`
  } catch (err) {
    const alert = $('#create-alert')
    const networkError = !(err instanceof Error && 'status' in err)
    if (networkError) {
      // 创建结果待确认(§3 列表状态):请求可能已到达服务端,
      // 保留请求标识,续查同一次创建(A01),不新建第二份
      btn.textContent = '查询创建结果'
      alert.textContent = '创建结果待确认:网络请求未收到回应,服务端可能已创建。点击「查询创建结果」核对同一请求;确认未创建后可放弃本次标识重新新建。'
    } else {
      // 服务端明确拒绝:本次 key 作废,下次用新 key
      pendingCreateKey = null
      btn.textContent = '新建画布'
      alert.textContent = `创建失败:${err instanceof Error ? err.message : String(err)}`
    }
    alert.hidden = false
    creating = false
    btn.disabled = false
  }
}

/* ---------- 概览:最近文档 + 最近画布直达 ---------- */

async function loadRecent() {
  const row = $('#recent-docs')
  try {
    const body = await api('/api/documents')
    const docs = (body.documents || []).slice(0, 3)
    row.innerHTML = ''
    if (docs.length === 0) {
      row.textContent = '暂无文档;切到「画布」Tab 新建第一幅。'
      row.className = 'recent-row ws-state'
      $('#ov-latest-name').textContent = '暂无画布,去新建'
      $('#ov-latest-canvas').href = '/workspace?tab=canvas'
      return
    }
    for (const doc of docs) {
      const card = document.createElement('a')
      card.className = 'recent-card'
      card.href = `/canvas?doc=${encodeURIComponent(doc.id)}`
      const name = document.createElement('b')
      name.textContent = doc.name || '未命名画布'
      const meta = document.createElement('span')
      meta.textContent = `${doc.objectCount} 个对象 · ${timeAgo(doc.updatedAt)}`
      card.append(name, meta)
      row.append(card)
    }
    $('#ov-latest-name').textContent = docs[0].name || '未命名画布'
    $('#ov-latest-canvas').href = `/canvas?doc=${encodeURIComponent(docs[0].id)}`
  } catch {
    row.innerHTML = ''
    const state = document.createElement('div')
    state.className = 'ws-state error'
    state.textContent = '最近文档读取失败'
    row.append(state)
  }
}
