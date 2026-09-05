const $ = (sel) => document.querySelector(sel)

const state = {
  history: [],
  activeJobs: new Map(),
  recentJobs: [],
  lbIndex: -1,
  initDataUrl: null,
  filter: 'all',
}

/* ---------- 基础工具 ---------- */

function toast(message, kind = 'info') {
  const el = document.createElement('div')
  el.className = `toast ${kind}`
  el.textContent = message
  $('#toasts').appendChild(el)
  requestAnimationFrame(() => el.classList.add('show'))
  setTimeout(
    () => {
      el.classList.remove('show')
      setTimeout(() => el.remove(), 300)
    },
    kind === 'error' ? 6000 : 3000,
  )
}

async function api(path, options) {
  const res = await fetch(path, options)
  const body = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new Error(body.error || `请求失败 HTTP ${res.status}`)
  }
  return body
}

/* ---------- 参数表单 ---------- */

function readParams() {
  const prompt = $('#prompt').value.trim()
  if (!prompt) {
    throw new Error('请先输入提示词')
  }
  const seedRaw = Number($('#seed').value)
  return {
    prompt,
    negativePrompt: $('#negative-prompt').value.trim(),
    model: $('#model').value,
    width: Number($('#width').value) || 512,
    height: Number($('#height').value) || 512,
    steps: Number($('#steps').value),
    cfgScale: Number($('#cfg').value),
    seed: Number.isFinite(seedRaw) ? Math.floor(seedRaw) : -1,
    batchCount: Math.max(1, Math.min(16, Number($('#batch').value) || 1)),
    sampler: $('#sampler').value || 'euler',
    scheduler: $('#scheduler').value || 'normal',
    denoise: Number($('#denoise').value) || 1,
    kind: $('#kind').value === 'video' ? 'video' : 'image',
    durationSec: Number($('#duration').value) || 4,
    fps: Number($('#fps').value) || 16,
  }
}

function fillParams(p) {
  $('#prompt').value = p.prompt
  $('#negative-prompt').value = p.negativePrompt || ''
  if (p.model && [...$('#model').options].some((o) => o.value === p.model)) {
    $('#model').value = p.model
  }
  $('#width').value = p.width
  $('#height').value = p.height
  $('#steps').value = p.steps
  $('#cfg').value = p.cfgScale
  $('#seed').value = p.seed
  $('#batch').value = p.batchCount
  setSelectValue('#sampler', p.sampler)
  setSelectValue('#scheduler', p.scheduler)
  $('#denoise').value = p.denoise ?? 1
  $('#kind').value = p.kind === 'video' ? 'video' : 'image'
  $('#duration').value = p.durationSec ?? 4
  $('#fps').value = p.fps ?? 16
  updateModeUI()
  syncSliderLabels()
}

/** 下拉框没有该选项时(历史记录来自旧版本或后端不可用)动态补一个,保证值不丢失。 */
function setSelectValue(sel, value) {
  if (!value) {
    return
  }
  const el = $(sel)
  if (![...el.options].some((o) => o.value === value)) {
    const opt = document.createElement('option')
    opt.value = value
    opt.textContent = value
    el.appendChild(opt)
  }
  el.value = value
}

function syncSliderLabels() {
  $('#steps-val').textContent = $('#steps').value
  $('#cfg-val').textContent = $('#cfg').value
  $('#denoise-val').textContent = $('#denoise').value
  $('#duration-val').textContent = $('#duration').value
  $('#fps-val').textContent = $('#fps').value
}

/** 按生成模式切换参数区:视频隐藏采样参数、显示时长/帧率。 */
function updateModeUI() {
  const isVideo = $('#kind').value === 'video'
  for (const el of document.querySelectorAll('.image-only')) {
    el.classList.toggle('hidden', isVideo)
  }
  for (const el of document.querySelectorAll('.video-only')) {
    el.classList.toggle('hidden', !isVideo)
  }
  $('#generate').textContent = isVideo ? '🎬 生成视频' : '✨ 生成'
}

async function loadModels() {
  try {
    const { models } = await api('/api/models')
    const sel = $('#model')
    sel.innerHTML = ''
    for (const m of models) {
      const opt = document.createElement('option')
      opt.value = m.id
      opt.textContent = m.name
      sel.appendChild(opt)
    }
  } catch (err) {
    $('#model-hint').textContent = err.message
  }
}

async function loadSamplers() {
  try {
    const { samplers, schedulers } = await api('/api/samplers')
    fillSelect('#sampler', samplers, 'euler')
    fillSelect('#scheduler', schedulers, 'normal')
  } catch (err) {
    fillSelect('#sampler', ['euler'], 'euler')
    fillSelect('#scheduler', ['normal'], 'normal')
    $('#model-hint').textContent = err.message
  }
}

function fillSelect(sel, values, selected) {
  const el = $(sel)
  el.innerHTML = ''
  for (const v of values) {
    const opt = document.createElement('option')
    opt.value = v
    opt.textContent = v
    el.appendChild(opt)
  }
  el.value = values.includes(selected) ? selected : values[0]
}

/* ---------- 图生图参考图 ---------- */

async function loadInitFile(file) {
  if (!file) {
    return
  }
  if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) {
    toast('仅支持 PNG / JPEG / WebP 图片', 'error')
    return
  }
  try {
    state.initDataUrl = await downscaleToDataUrl(file, 1024)
    $('#init-preview').src = state.initDataUrl
    $('#init-preview').classList.remove('hidden')
    $('#drop-hint').classList.add('hidden')
    $('#init-clear').classList.remove('hidden')
    if ($('#kind').value === 'image' && Number($('#denoise').value) >= 1) {
      $('#denoise').value = 0.6
      syncSliderLabels()
      toast('已载入参考图,重绘幅度设为 0.6,可自行调整')
    }
  } catch (err) {
    toast(`读取图片失败:${err.message}`, 'error')
  }
}

function clearInitImage() {
  state.initDataUrl = null
  $('#init-file').value = ''
  $('#init-preview').classList.add('hidden')
  $('#drop-hint').classList.remove('hidden')
  $('#init-clear').classList.add('hidden')
}

/** 大图缩放到 maxSize 内再转 JPEG data URL,控制上传体积。 */
function downscaleToDataUrl(file, maxSize) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(new Error('无法读取文件'))
    reader.onload = () => {
      const img = new Image()
      img.onerror = () => reject(new Error('无法解析图片'))
      img.onload = () => {
        const scale = Math.min(1, maxSize / Math.max(img.width, img.height))
        const canvas = document.createElement('canvas')
        canvas.width = Math.max(8, Math.round(img.width * scale))
        canvas.height = Math.max(8, Math.round(img.height * scale))
        canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height)
        resolve(canvas.toDataURL('image/jpeg', 0.9))
      }
      img.src = reader.result
    }
    reader.readAsDataURL(file)
  })
}

/* ---------- 参数预设(localStorage) ---------- */

const PRESET_KEY = 'swarmui-presets'

function loadPresets() {
  try {
    const raw = JSON.parse(localStorage.getItem(PRESET_KEY) || '{}')
    return raw && typeof raw === 'object' ? raw : {}
  } catch {
    return {}
  }
}

function savePresets(presets) {
  try {
    localStorage.setItem(PRESET_KEY, JSON.stringify(presets))
  } catch {
    toast('预设保存失败(localStorage 不可用)', 'error')
  }
}

function renderPresetSelect() {
  const presets = loadPresets()
  const sel = $('#preset-select')
  sel.innerHTML = '<option value="">选择预设…</option>'
  for (const name of Object.keys(presets)) {
    const opt = document.createElement('option')
    opt.value = name
    opt.textContent = name
    sel.appendChild(opt)
  }
}

/* ---------- 渲染 ---------- */

/** 按过滤条件返回可见历史:全部 / 图像 / 视频 / 收藏。 */
function visibleHistory() {
  if (state.filter === 'image') {
    return state.history.filter((h) => h.params.kind !== 'video')
  }
  if (state.filter === 'video') {
    return state.history.filter((h) => h.params.kind === 'video')
  }
  if (state.filter === 'star') {
    return state.history.filter((h) => h.starred)
  }
  return state.history
}

/** 网格单元的媒体元素:视频记录渲染 <video>(动画 SVG 除外),其余渲染 <img>。 */
function createCellMedia(record) {
  if (record.params.kind === 'video' && !record.url.endsWith('.svg')) {
    const v = document.createElement('video')
    v.src = record.url
    v.muted = true
    v.loop = true
    v.playsInline = true
    v.autoplay = true
    return v
  }
  const im = document.createElement('img')
  im.loading = 'lazy'
  im.src = record.url
  im.alt = record.params.prompt
  return im
}

/** 缩略图左上角的参数徽章行:模式 + 尺寸。 */
function createBadges(record) {
  const badges = document.createElement('div')
  badges.className = 'cell-badges'
  const kind = document.createElement('span')
  kind.textContent =
    record.params.kind === 'video'
      ? `视频 ${record.params.durationSec}s`
      : record.params.denoise < 1
        ? '图生图'
        : '文生图'
  const size = document.createElement('span')
  size.textContent = `${record.params.width}×${record.params.height}`
  badges.append(kind, size)
  return badges
}

function renderGrid() {
  const grid = $('#grid')
  const items = visibleHistory()
  grid.replaceChildren()
  for (const img of items) {
    const cell = document.createElement('div')
    cell.className = 'cell'
    const media = createCellMedia(img)
    const star = document.createElement('button')
    star.className = `cell-star${img.starred ? ' on' : ''}`
    star.textContent = '★'
    star.title = img.starred ? '取消收藏' : '收藏'
    star.addEventListener('click', (e) => {
      e.stopPropagation()
      toggleStar(img)
    })
    const overlay = document.createElement('div')
    overlay.className = 'cell-overlay'
    const seedSpan = document.createElement('span')
    seedSpan.textContent = `#${img.params.seed}`
    const sizeSpan = document.createElement('span')
    const kindLabel =
      img.params.kind === 'video' ? ' · 视频' : img.params.denoise < 1 ? ' · img2img' : ''
    sizeSpan.textContent = `${img.params.width}×${img.params.height}${kindLabel}`
    overlay.append(seedSpan, sizeSpan)
    cell.append(media, createBadges(img), star, overlay)
    cell.addEventListener('click', () => openLightbox(img.id))
    grid.appendChild(cell)
  }
  const total = state.history.length
  $('#empty').style.display = total ? 'none' : ''
  $('#empty').textContent = total
    ? '当前过滤条件下没有记录'
    : '还没有生成记录 — 在左侧输入提示词,点击「生成」试试'
  $('#history-count').textContent = total
    ? items.length === total
      ? `共 ${total} 张`
      : `${items.length} / ${total} 张`
    : ''
}

function tagOf(text) {
  const tag = document.createElement('span')
  tag.className = 'mode-tag'
  tag.textContent = text
  return tag
}

/** 用原参数重新提交一次生成(去掉固定种子以保留随机性)。 */
async function retryJob(job) {
  const { seed: _dropped, ...rest } = job.params
  try {
    await api('/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(rest),
    })
    toast('已重新排队')
  } catch (err) {
    toast(err.message, 'error')
  }
}

/** 单个任务行:进行中显示进度+取消;失败显示错误+重试。 */
function renderJobRow(job) {
  const pct = Math.round(job.progress * 100)
  const row = document.createElement('div')
  row.className = 'job'
  const info = document.createElement('div')
  info.className = 'job-info'
  const promptEl = document.createElement('div')
  promptEl.className = 'job-prompt'
  promptEl.textContent = job.params.prompt
  info.append(promptEl)
  if (job.params.kind === 'video') {
    info.appendChild(tagOf('视频'))
  } else if (job.hasInitImage) {
    info.appendChild(tagOf('图生图'))
  }
  const hint = document.createElement('span')
  hint.className = 'hint'
  hint.textContent = job.status === 'queued' ? '排队中…' : `${job.message} · ${pct}%`
  info.appendChild(hint)
  row.append(info)

  if (job.status === 'failed' && job.error && job.error !== '已取消') {
    const errEl = document.createElement('div')
    errEl.className = 'job-error'
    errEl.textContent = `失败:${job.error}`
    row.appendChild(errEl)
    const retry = document.createElement('button')
    retry.className = 'job-retry'
    retry.textContent = '↻ 重试'
    retry.addEventListener('click', () => retryJob(job))
    row.appendChild(retry)
  } else {
    const cancel = document.createElement('button')
    cancel.className = 'job-cancel'
    cancel.textContent = '✕'
    cancel.title = '取消任务'
    cancel.addEventListener('click', async () => {
      try {
        await api(`/api/jobs/${job.id}`, { method: 'DELETE' })
      } catch (err) {
        toast(err.message, 'error')
      }
    })
    row.appendChild(cancel)
    const bar = document.createElement('div')
    bar.className = 'bar'
    const fill = document.createElement('div')
    fill.className = 'bar-fill'
    fill.style.width = `${pct}%`
    bar.appendChild(fill)
    row.appendChild(bar)
  }
  return row
}

function renderJobs() {
  const wrap = $('#jobs')
  wrap.replaceChildren()
  for (const job of state.activeJobs.values()) {
    wrap.appendChild(renderJobRow(job))
  }
}

/** 任务历史面板:最近任务状态一览,失败项可就地重试。 */
function renderJobHistory() {
  const wrap = $('#job-history')
  wrap.replaceChildren()
  if (state.recentJobs.length === 0) {
    wrap.classList.add('hidden')
    return
  }
  wrap.classList.remove('hidden')
  for (const job of state.recentJobs.slice(0, 12)) {
    const row = document.createElement('div')
    row.className = 'jh-row'
    const dot = document.createElement('span')
    dot.className = `jh-dot jh-${job.status}`
    const prompt = document.createElement('span')
    prompt.className = 'jh-prompt'
    prompt.textContent = job.params.prompt
    prompt.title = job.params.prompt
    const status = document.createElement('span')
    status.className = 'hint'
    status.textContent =
      job.status === 'completed'
        ? `✓ ${job.images.length} 张`
        : job.status === 'failed'
          ? `✗ ${job.error || '失败'}`
          : job.status === 'running'
            ? '进行中'
            : '排队中'
    row.append(dot, prompt, status)
    if (job.status === 'failed') {
      const retry = document.createElement('button')
      retry.className = 'jh-retry'
      retry.textContent = '重试'
      retry.addEventListener('click', () => retryJob(job))
      row.appendChild(retry)
    }
    wrap.appendChild(row)
  }
}

function renderAll() {
  renderGrid()
  renderJobs()
  renderJobHistory()
}

/* ---------- 灯箱 ---------- */

function openLightbox(id) {
  state.lbIndex = state.history.findIndex((h) => h.id === id)
  if (state.lbIndex === -1) {
    return
  }
  $('#lightbox').classList.remove('hidden')
  renderLightbox()
}

function closeLightbox() {
  const v = $('#lb-video')
  if (v) {
    v.pause()
  }
  $('#lightbox').classList.add('hidden')
  state.lbIndex = -1
}

function renderLightbox() {
  const img = state.history[state.lbIndex]
  if (!img) {
    return closeLightbox()
  }
  // 视频记录用 <video> 播放;动画 SVG 占位产物仍用 <img>(自身会动)
  const isVideo = img.params.kind === 'video' && !img.url.endsWith('.svg')
  const videoEl = $('#lb-video')
  videoEl.classList.toggle('hidden', !isVideo)
  $('#lb-img').classList.toggle('hidden', isVideo)
  if (isVideo) {
    videoEl.src = img.url
  } else {
    videoEl.pause()
    videoEl.removeAttribute('src')
    $('#lb-img').src = img.url
  }
  $('#lb-caption').textContent = img.params.prompt
  const p = img.params
  const rows = [
    ['类型', p.kind === 'video' ? `视频 · ${p.durationSec ?? 4}s / ${p.fps ?? 16}fps` : '图像'],
    ['模型', p.model],
    ['尺寸', `${p.width} × ${p.height}`],
    ...(p.kind === 'video'
      ? []
      : [
          ['步数', String(p.steps)],
          ['CFG', String(p.cfgScale)],
          ['采样器', p.sampler ? `${p.sampler} / ${p.scheduler ?? ''}` : '—'],
          ['重绘幅度', p.denoise != null ? `${p.denoise}${p.denoise < 1 ? '(图生图)' : ''}` : '—'],
        ]),
    ['种子', String(p.seed)],
    ['反向提示词', p.negativePrompt || '—'],
    ['后端', img.provider],
    ['时间', new Date(img.createdAt).toLocaleString()],
  ]
  const paramsEl = $('#lb-params')
  paramsEl.replaceChildren()
  for (const [k, v] of rows) {
    const kv = document.createElement('div')
    kv.className = 'kv'
    const kEl = document.createElement('span')
    kEl.className = 'k'
    kEl.textContent = k
    const vEl = document.createElement('span')
    vEl.className = 'v'
    vEl.textContent = v
    kv.append(kEl, vEl)
    paramsEl.appendChild(kv)
  }
  $('#lb-download').href = img.url
  $('#lb-download').setAttribute('download', img.file)
  const starBtn = $('#lb-star')
  starBtn.textContent = img.starred ? '★ 已收藏' : '☆ 收藏'
  starBtn.classList.toggle('on', Boolean(img.starred))
}

/** 收藏 / 取消收藏;失败时回滚按钮态并提示。 */
async function toggleStar(record) {
  const next = !record.starred
  try {
    await api(`/api/images/${record.id}/star`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ starred: next }),
    })
    record.starred = next
    renderGrid()
    if (state.lbIndex !== -1 && state.history[state.lbIndex]?.id === record.id) {
      renderLightbox()
    }
  } catch (err) {
    toast(err.message, 'error')
  }
}

/** 把当前过滤结果逐个触发下载(浏览器对多文件下载会询问一次授权)。 */
function downloadAll() {
  const items = visibleHistory()
  if (items.length === 0) {
    toast('当前过滤条件下没有可下载的记录', 'error')
    return
  }
  for (const item of items) {
    const a = document.createElement('a')
    a.href = item.url
    a.download = item.file
    document.body.appendChild(a)
    a.click()
    a.remove()
  }
  toast(`已触发下载 ${items.length} 个文件`)
}

function navLightbox(step) {
  if (state.lbIndex === -1 || state.history.length === 0) {
    return
  }
  state.lbIndex = (state.lbIndex + step + state.history.length) % state.history.length
  renderLightbox()
}

async function deleteCurrentImage() {
  const img = state.history[state.lbIndex]
  if (!img) {
    return
  }
  try {
    await api(`/api/images/${img.id}`, { method: 'DELETE' })
    state.history = state.history.filter((h) => h.id !== img.id)
    toast('已删除')
    renderGrid()
    if (state.history.length) {
      state.lbIndex = Math.min(state.lbIndex, state.history.length - 1)
      renderLightbox()
    } else {
      closeLightbox()
    }
  } catch (err) {
    toast(err.message, 'error')
  }
}

/* ---------- 实时事件(SSE) ---------- */

function connectEvents() {
  const es = new EventSource('/api/events')
  es.addEventListener('snapshot', (e) => {
    const data = JSON.parse(e.data)
    state.history = data.history
    state.activeJobs = new Map(data.jobs.map((j) => [j.id, j]))
    renderAll()
  })
  es.addEventListener('job', (e) => {
    const job = JSON.parse(e.data)
    // 任务历史面板同步:新增或更新最近任务列表
    state.recentJobs = [job, ...state.recentJobs.filter((j) => j.id !== job.id)].slice(0, 30)
    if (job.status === 'queued' || job.status === 'running') {
      state.activeJobs.set(job.id, job)
    } else {
      state.activeJobs.delete(job.id)
      if (job.status === 'failed') {
        if (job.error === '已取消') {
          toast('任务已取消')
        } else {
          toast(`生成失败:${job.error}`, 'error')
        }
      } else if (job.status === 'completed') {
        toast(`生成完成,共 ${job.images.length} 张`)
      }
    }
    renderJobs()
    renderJobHistory()
  })
  es.addEventListener('image', (e) => {
    const { image } = JSON.parse(e.data)
    state.history = state.history.filter((h) => h.id !== image.id)
    state.history.unshift(image)
    renderGrid()
  })
  es.onerror = () => setPill(false, '连接中断,重连中…')
}

/* ---------- 状态指示 ---------- */

function setPill(ok, text) {
  $('#backend-text').textContent = text
  $('#backend-pill').classList.toggle('bad', !ok)
}

async function pollHealth() {
  try {
    const { provider, backend } = await api('/api/health')
    const label = provider === 'mock' ? '内置演示后端' : 'ComfyUI'
    setPill(backend.ok, backend.ok ? label : `${label} · 不可用`)
    $('#model-hint').textContent = backend.ok ? '' : backend.detail
  } catch {
    setPill(false, '服务不可达')
  }
}

/* ---------- 初始化 ---------- */

function bindUI() {
  $('#generate').addEventListener('click', async () => {
    $('#form-error').textContent = ''
    let params
    try {
      params = readParams()
    } catch (err) {
      $('#form-error').textContent = err.message
      return
    }
    try {
      await api('/api/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...params, initImage: state.initDataUrl || undefined }),
      })
      toast('已加入生成队列')
    } catch (err) {
      $('#form-error').textContent = err.message
    }
  })

  $('#prompt').addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      $('#generate').click()
    }
  })

  $('#steps').addEventListener('input', syncSliderLabels)
  $('#cfg').addEventListener('input', syncSliderLabels)
  $('#denoise').addEventListener('input', syncSliderLabels)
  $('#duration').addEventListener('input', syncSliderLabels)
  $('#fps').addEventListener('input', syncSliderLabels)
  $('#kind').addEventListener('change', updateModeUI)

  $('#size-preset').addEventListener('change', (e) => {
    if (!e.target.value) {
      return
    }
    const [w, h] = e.target.value.split('x').map(Number)
    $('#width').value = w
    $('#height').value = h
  })

  // 图生图:点击选择 / 拖拽 / 移除
  $('#drop-zone').addEventListener('click', () => $('#init-file').click())
  $('#init-file').addEventListener('change', (e) => loadInitFile(e.target.files[0]))
  $('#init-clear').addEventListener('click', (e) => {
    e.stopPropagation()
    clearInitImage()
  })
  for (const evt of ['dragover', 'dragenter']) {
    $('#drop-zone').addEventListener(evt, (e) => {
      e.preventDefault()
      $('#drop-zone').classList.add('dragover')
    })
  }
  for (const evt of ['dragleave', 'drop']) {
    $('#drop-zone').addEventListener(evt, (e) => {
      e.preventDefault()
      $('#drop-zone').classList.remove('dragover')
    })
  }
  $('#drop-zone').addEventListener('drop', (e) => {
    loadInitFile(e.dataTransfer?.files?.[0])
  })

  // 预设:保存 / 载入 / 删除
  $('#preset-save').addEventListener('click', () => {
    const name = $('#preset-name').value.trim() || $('#preset-select').value
    if (!name) {
      return toast('请先填写预设名称', 'error')
    }
    const presets = loadPresets()
    presets[name] = readParams()
    savePresets(presets)
    $('#preset-name').value = ''
    renderPresetSelect()
    $('#preset-select').value = name
    toast(`预设「${name}」已保存`)
  })
  $('#preset-select').addEventListener('change', (e) => {
    const preset = loadPresets()[e.target.value]
    if (preset) {
      fillParams(preset)
      toast(`已载入预设「${e.target.value}」`)
    }
  })
  $('#preset-delete').addEventListener('click', () => {
    const name = $('#preset-select').value
    if (!name) {
      return toast('请先在下拉框选中要删除的预设', 'error')
    }
    const presets = loadPresets()
    delete presets[name]
    savePresets(presets)
    renderPresetSelect()
    toast(`预设「${name}」已删除`)
  })

  $('#lb-close').addEventListener('click', closeLightbox)
  $('#lb-prev').addEventListener('click', () => navLightbox(-1))
  $('#lb-next').addEventListener('click', () => navLightbox(1))
  $('#lb-delete').addEventListener('click', deleteCurrentImage)
  $('#lb-star').addEventListener('click', () => {
    const img = state.history[state.lbIndex]
    if (img) {
      toggleStar(img)
    }
  })

  // 结果工具栏:过滤 + 全部下载
  for (const btn of document.querySelectorAll('.chip[data-filter]')) {
    btn.addEventListener('click', () => {
      state.filter = btn.dataset.filter
      for (const b of document.querySelectorAll('.chip[data-filter]')) {
        b.classList.toggle('active', b === btn)
      }
      renderGrid()
    })
  }
  $('#download-all').addEventListener('click', downloadAll)

  // 任务历史面板折叠
  $('#job-history-toggle').addEventListener('toggle', (e) => {
    $('#job-history').classList.toggle('hidden', !e.target.open)
  })
  $('#lb-reuse').addEventListener('click', () => {
    const img = state.history[state.lbIndex]
    if (!img) {
      return
    }
    fillParams(img.params)
    closeLightbox()
    $('.sidebar').scrollIntoView({ behavior: 'smooth' })
    toast('参数已填入,可继续调整后生成')
  })
  $('#lb-reuse-seed').addEventListener('click', () => {
    const img = state.history[state.lbIndex]
    if (!img) {
      return
    }
    $('#seed').value = img.params.seed
    toast(`种子 ${img.params.seed} 已填入`)
  })
  $('#lightbox').addEventListener('click', (e) => {
    if (e.target === e.currentTarget) {
      closeLightbox()
    }
  })

  document.addEventListener('keydown', (e) => {
    if ($('#lightbox').classList.contains('hidden')) {
      return
    }
    if (e.key === 'Escape') {
      closeLightbox()
    }
    if (e.key === 'ArrowLeft') {
      navLightbox(-1)
    }
    if (e.key === 'ArrowRight') {
      navLightbox(1)
    }
    if ((e.key === 'f' || e.key === 'F') && !e.metaKey && !e.ctrlKey) {
      const img = state.history[state.lbIndex]
      if (img) {
        toggleStar(img)
      }
    }
  })
}

async function init() {
  bindUI()
  syncSliderLabels()
  updateModeUI()
  renderPresetSelect()
  await Promise.all([loadModels(), loadSamplers(), pollHealth()])
  try {
    const [{ images }, { jobs: active }, { jobs: recent }] = await Promise.all([
      api('/api/history'),
      api('/api/jobs'),
      api('/api/jobs?all=1&limit=30'),
    ])
    state.history = images
    state.activeJobs = new Map(active.map((j) => [j.id, j]))
    state.recentJobs = recent
  } catch (err) {
    toast(err.message, 'error')
  }
  renderAll()
  connectEvents()
  setInterval(pollHealth, 8000)
}

init()
