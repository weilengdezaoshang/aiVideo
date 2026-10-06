// v0.5 视觉扩展：在原型 IIFE 内、product-extension.js 后执行。
// 仅替换呈现，不建立第二套生成、蒙版、保存或导航状态。
const visualKeys = ['architecture', 'flower', 'ocean', 'tea']
const visualPreviousEditor = editor
const visualPreviousTile = tile
const visualPreviousGroup = group
const visualPreviousMaskInfo = maskInfo
const visualPreviousConversation = conversation
const visualPreviousRender = render
let visualEditorTheme = 'light'

function applyVisualEditorTheme() {
  document.documentElement.dataset.editorTheme =
    model.page === 'editor' ? visualEditorTheme : 'light'
}

window.addEventListener('message', (event) => {
  if (event.source !== window.parent || event.data?.type !== 'product-theme') {
    return
  }
  visualEditorTheme = event.data.theme === 'dark' ? 'dark' : 'light'
  applyVisualEditorTheme()
})

function visualIcon(name, extra = '') {
  const value = typeof ICONS !== 'undefined' ? ICONS[name] : ''
  return value ? `<span class="v-icon ${extra}" aria-hidden="true">${value}</span>` : ''
}

function visualAsset(key) {
  return typeof VISUAL_ASSETS !== 'undefined' ? VISUAL_ASSETS[key] || '' : ''
}

function visualPicture(key, className, alt = '') {
  const value = visualAsset(key)
  return value
    ? `<img class="${className}" src="${esc(value)}" alt="${esc(alt)}" draggable="false">`
    : ''
}

function visualRail() {
  return `<nav class="rail visual-rail" aria-label="工作台模块">
    <div class="visual-nav-label">创作空间</div>
    ${[
      ['home', 'home', '工作台'],
      ['list', 'grid', '我的画布'],
      ['legacy', 'image', '图片'],
      ['video-entry', 'video', '视频'],
    ]
      .map(
        ([action, icon, label]) =>
          `<button data-act="${action}" class="${model.page === action ? 'active' : ''}" ${model.page === action ? 'aria-current="page"' : ''}>${visualIcon(icon)}<span>${label}</span>${model.page === action ? '<span class="nav-current-mark" aria-hidden="true"></span>' : ''}</button>`,
      )
      .join('')}
    <div class="visual-rail-footer"><span class="local-dot" aria-hidden="true"></span><div>本机创作空间<small>交互原型 · 示例数据</small></div></div>
  </nav>`
}

function visualDocCard(d, index) {
  const stableIndex = Array.from(d.id).reduce(
    (value, char) => (value * 31 + char.charCodeAt(0)) >>> 0,
    0,
  )
  const key = visualKeys[stableIndex % visualKeys.length]
  const preview = visualPicture(key, 'visual-doc-image', '画布的演示封面')
  return `<button class="doc-card visual-doc-card" data-open="${esc(d.id)}">
    <span class="doc-cover">${preview}<span class="visual-cover-note">演示封面</span><span class="visual-cover-open">${visualIcon('arrow')}<span>打开画布</span></span></span>
    <span class="visual-doc-title">${visualIcon('grid')}<b>${esc(d.name)}</b></span>
    <span class="visual-doc-meta">${d.save === 'error' ? '有修改待保存' : '已保存在本机演练'}<span>${d.save === 'error' ? '待保存' : '画布文档'}</span></span>
  </button>`
}

function visualWorkspace() {
  const recent = model.docs.slice(-3).reverse()
  return `<main class="hub visual-hub"><div class="visual-content">
    <header class="visual-page-heading"><div><h1>今天，想创作什么？</h1><p>留住一个想法，慢慢展开它的不同可能。</p></div><span class="visual-space-label">${visualIcon('folder')}你的创作空间</span></header>
    <section class="workspace-feature visual-workspace-feature">
      <div class="visual-feature-copy"><h2>让创意，<br>在画布中展开。</h2><p>生成四个方向，选一张继续调整。<br>素材、灵感与对话，都留在同一处。</p><button class="primary" data-act="list">进入我的画布${visualIcon('arrow')}</button><small>从空白开始，也可以导入已有素材</small></div>
      <div class="visual-feature-image">${visualPicture('architecture', 'visual-feature-photo', '空间建筑氛围展示')}<span class="visual-image-caption">氛围素材展示 · 非模型生成结果</span></div>
    </section>
    <section class="visual-recent"><div class="workspace-section-title"><h2>最近的画布</h2><button class="visual-text-button" data-act="list">查看全部${visualIcon('arrow')}</button></div>
      ${recent.length ? `<div class="doc-grid">${recent.map(visualDocCard).join('')}</div>` : `<div class="visual-recent-empty">${visualIcon('grid')}<div><h3>这里会留下你的创作</h3><p>先创建一张画布，下次回来就能接着完成。</p></div><button data-act="list">进入我的画布${visualIcon('arrow')}</button></div>`}
    </section>
    <section class="visual-capabilities" aria-label="其他素材入口"><div>${visualIcon('image')}<span><strong>图片创作</strong><small>四个方向，在画布中比较与调整</small></span><button data-act="list" aria-label="去画布生成图片">${visualIcon('arrow')}</button></div><div>${visualIcon('video')}<span><strong>视频素材</strong><small>已有生成入口与素材预览的接入说明</small></span><button data-act="video-entry" aria-label="查看视频入口说明">${visualIcon('arrow')}</button></div></section>
  </div></main>`
}

function visualCanvasList() {
  const docs = model.docs.slice().reverse()
  return `<main class="hub visual-hub"><div class="visual-content">
    <header class="visual-page-heading"><div><h1>我的画布</h1><p>每一个想法，都有继续生长的地方。</p></div><button class="primary visual-new-canvas" data-act="new">${visualIcon('plus')}新建画布</button></header>
    ${docs.length ? `<div class="visual-list-topline"><span>全部画布</span><small>本机演练文档</small></div><div class="doc-grid visual-full-grid">${docs.map(visualDocCard).join('')}</div><p class="visual-list-footnote">封面为视觉演示素材；打开后查看对应文档的实际演练内容。</p>` : `<section class="visual-empty-canvas"><div class="visual-empty-copy">${visualIcon('grid', 'visual-empty-icon')}<h2>第一张画布，<br>从这里开始。</h2><p>写下一句描述，比较四个创意方向；<br>再把喜欢的那张，变成下一步的起点。</p><button class="primary" data-act="new">${visualIcon('plus')}创建第一张画布</button><small>新建不会自动发起生成</small></div><div class="visual-empty-art">${visualPicture('flower', 'visual-empty-photo', '花卉氛围展示')}<span class="visual-image-caption">氛围素材 · 非画布作品</span></div></section>`}
  </div></main>`
}

hub = function () {
  return `<div class="shell-body">${visualRail()}${model.page === 'list' ? visualCanvasList() : visualWorkspace()}</div>${sheet()}`
}

editor = function (d) {
  return visualPreviousEditor(d)
    .replace('<div class="appbar">', '<div class="appbar visual-editor-bar">')
    .replace(
      '<button data-act="back">返回我的画布</button>',
      `<button data-act="back" class="visual-back-button">${visualIcon('arrow', 'visual-back-icon')}<span>我的画布</span></button>`,
    )
    .replace(
      '<header>对话 <small style="float:right">当前画布</small></header>',
      `<header class="visual-conversation-heading"><span>${visualIcon('sparkles')}创作对话</span><small>当前画布</small></header>`,
    )
    .replace('<small>方向占位 · 非真实生成</small>', '<small>演示素材 · 非生成结果</small>')
}

tile = function (o) {
  const markup = visualPreviousTile(o)
  if (o.status !== 'success' || o.kind !== 'image') {
    return markup
  }
  const inMask = doc()?.mask?.source === o.id
  if (inMask) {
    return markup.replace(
      /<span class="caption">[\s\S]*?<\/span>/,
      '<span class="caption visual-mask-placeholder">选区交互示意<small>非照片真实识别</small></span>',
    )
  }
  // 仅四候选展示氛围照片；导入的“人物”占位不能用不相关素材冒充。
  if (o.slot === undefined || !visualAsset(visualKeys[o.slot % visualKeys.length])) {
    return markup
  }
  const image = visualPicture(
    visualKeys[o.slot % visualKeys.length],
    'visual-candidate-image',
    '演示素材，非当前提示词生成结果',
  )
  return markup
    .replace('<span class="cross"></span>', image)
    .replace(
      /<span class="caption">[\s\S]*?<\/span>/,
      '<span class="caption visual-candidate-caption">演示素材<small>非生成结果</small></span>',
    )
}

group = function (g) {
  const markup = visualPreviousGroup(g)
  if (
    g.items.some((o) => o.status === 'success') &&
    !g.items.some((o) => doc()?.mask?.source === o.id)
  ) {
    return markup.replace(
      '<div class="image-row">',
      '<div class="visual-group-note">画面仅演示布局，不对应当前提示词</div><div class="image-row">',
    )
  }
  return markup
}

maskInfo = function (m) {
  return visualPreviousMaskInfo(m).replace(
    '</div>',
    '<span class="visual-mask-disclaimer">演示选区用于体验操作步骤，未对照片执行真实识别。</span></div>',
  )
}

conversation = function (d) {
  const markup = visualPreviousConversation(d)
  const content = document.createElement('template')
  content.innerHTML = markup
  // 两处预览使用同一演示素材映射，保留原消息与定位动作。
  content.content.querySelectorAll('.result-preview').forEach((preview, groupIndex) => {
    const candidates = d.groups[groupIndex]?.items.filter((o) => o.slot !== undefined) || []
    preview.querySelectorAll('.mini').forEach((mini, index) => {
      const candidate = candidates[index]
      if (!candidate || candidate.status !== 'success') {
        return
      }
      const key = visualKeys[candidate.slot % visualKeys.length]
      if (!visualAsset(key)) {
        return
      }
      mini.classList.add('visual-mini-photo')
      mini.innerHTML = `${visualPicture(key, 'visual-mini-image', '与画布对应的演示素材，非生成结果')}<span>演示素材 ${candidate.slot + 1}</span>`
    })
  })
  return content.innerHTML
}

render = function () {
  visualPreviousRender()
  app.dataset.visual = 'v05'
  applyVisualEditorTheme()
  const empty = app.querySelector('.conversation-body')
  if (empty && !doc()?.messages.length && !doc()?.groups.length && !doc()?.agent) {
    empty.innerHTML = `<div class="visual-chat-empty">${visualIcon('sparkles')}<h3>创意从一句话开始</h3><p>在画布下方描述你的想法。<br>对话与每次创作的结果都会留在这里。</p></div>`
  }
  const review = document.querySelector('.review-controls')
  if (review) {
    review.setAttribute('aria-label', '演示控制')
  }
  if (model.page === 'editor' && doc()) {
    renderTools(doc())
  }
}

render()
