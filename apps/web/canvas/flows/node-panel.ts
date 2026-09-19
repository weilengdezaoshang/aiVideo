import { prepareNodeDraft } from './node-request.js'
import { pickReferenceAsset } from './reference-picker.js'
import { createActionMenu } from '../../shared/action-menu.js'
import { createDropdown } from '../../shared/dropdown.js'
import { isEmptyMedia } from '../engine/node-appearance.js'
import type { CanvasObj } from '../state/commands.js'
import { cmdAddObjects, cmdUpdateObject } from '../state/commands.js'
import type { createDocStore } from '../state/doc-store.js'
import type { createCanvasEngine } from '../engine/stage.js'
import { createNode, hasOutput, isBusy, isMediaNode } from '../state/node-model.js'
import type { Capabilities, NodeDraft, Reference } from '../state/node-model.js'
import { createNodeGeneration } from './node-generation.js'
import { ensureAsset } from './asset-util.js'

type Options = {
  store: ReturnType<typeof createDocStore>
  engine: ReturnType<typeof createCanvasEngine>
  getSelection: () => string[]
  select: (id: string) => void
  pickObject: (point: { x: number; y: number }) => string | null
  editText: (id: string) => void
  notify: (message: string) => void
}

export function createNodePanel(options: Options) {
  // Older cached HTML may load the new module without its companion stylesheet.
  if (!document.getElementById('node-panel-styles')) {
    const stylesheet = document.createElement('link')
    stylesheet.id = 'node-panel-styles'
    stylesheet.rel = 'stylesheet'
    stylesheet.href = '/canvas/node-panel.css?v=2'
    document.head.append(stylesheet)
  }
  const { store, engine, getSelection, select, editText, notify } = options
  const generation = createNodeGeneration(store, notify)
  const panel = document.createElement('section')
  panel.className = 'node-panel'
  panel.hidden = true
  panel.setAttribute('aria-label', '节点创作面板')
  panel.innerHTML = `
    <div class="node-panel-top"><span class="node-panel-title"></span><button type="button" class="node-expand" aria-label="展开编辑" title="展开编辑"><sl-icon name="arrows-angle-expand"></sl-icon></button></div>
    <div class="node-references"><details class="node-reference-menu" id="node-reference-menu"><summary class="node-add-reference" aria-label="添加参考图" title="添加参考图"><sl-icon name="plus-lg" aria-hidden="true"></sl-icon></summary><div class="node-reference-popup"><button type="button" role="menuitem" class="node-upload" data-menu-icon="upload">本地上传</button><button type="button" role="menuitem" class="node-pick-asset" data-menu-icon="image">从资产选择</button><button type="button" role="menuitem" class="node-pick-canvas" data-menu-icon="ratio">从画布选择</button></div></details><div class="node-reference-items"></div></div>
    <textarea class="node-prompt" aria-label="生成描述" maxlength="4000"></textarea>
    <div class="node-panel-footer"><div class="node-settings">
      <label><span class="node-field-label">模型</span><select aria-label="模型" class="node-model" data-icon="model"></select></label>
      <details class="node-parameters" id="node-parameters"><summary aria-label="生成参数" data-menu-icon="settings"><span class="node-parameters-value"></span><span class="ui-dropdown-chevron"></span></summary><div class="node-parameters-popup">
      <label><span class="node-field-label">比例</span><select aria-label="比例" class="node-ratio" data-icon="ratio"></select></label>
      <label><span class="node-field-label">分辨率</span><select aria-label="分辨率" class="node-resolution" data-icon="resolution"></select></label>
      </div></details>
      <label class="node-duration-wrap"><span class="node-field-label">时长</span><select aria-label="时长" class="node-duration" data-icon="time"></select></label>
      <label class="node-denoise-wrap"><span class="node-field-label">重绘幅度</span><input aria-label="重绘幅度" class="node-denoise" type="number" min="0.05" max="1" step="0.05"></label>
    </div>
    <div class="node-panel-bottom"><span class="node-mode-hint"></span><button type="button" class="node-cancel">取消生成</button><button type="button" class="node-submit"><sl-icon name="arrow-up"></sl-icon><span>生成</span></button></div>
    </div><div class="node-status" role="status" aria-live="polite"></div>`
  const textBar = document.createElement('div')
  textBar.className = 'node-text-toolbar'
  textBar.hidden = true
  textBar.setAttribute('role', 'toolbar')
  textBar.setAttribute('aria-label', '文本工具栏')
  textBar.innerHTML = `<label><input type="color" class="node-text-color" aria-label="文本背景色">背景色</label><button type="button" class="node-text-edit">编辑文本</button><button type="button" class="node-text-expand" aria-label="展开文本" title="展开文本"><sl-icon name="arrows-angle-expand"></sl-icon></button><button type="button" class="node-text-download" aria-label="下载文本" title="下载文本"><sl-icon name="download"></sl-icon></button>`
  const dialog = document.createElement('dialog')
  dialog.className = 'node-text-dialog'
  dialog.innerHTML =
    '<form method="dialog"><h2>编辑文本</h2><textarea aria-label="文本内容" maxlength="20000"></textarea><div><button value="cancel">取消</button><button value="save">保存</button></div></form>'
  document.body.append(panel, textBar, dialog)
  const get = <T extends HTMLElement>(selector: string) => panel.querySelector<T>(selector)!
  const prompt = get<HTMLTextAreaElement>('.node-prompt')
  const model = get<HTMLSelectElement>('.node-model')
  const ratio = get<HTMLSelectElement>('.node-ratio')
  const resolution = get<HTMLSelectElement>('.node-resolution')
  const duration = get<HTMLSelectElement>('.node-duration')
  const denoise = get<HTMLInputElement>('.node-denoise')
  const dropdowns = new Map(
    [model, ratio, resolution, duration].map((control) => [control, createDropdown(control)]),
  )
  const referenceDetails = get<HTMLDetailsElement>('.node-reference-menu')
  const referenceMenu = createActionMenu(referenceDetails)
  referenceDetails.querySelector('summary')!.addEventListener('click', (event) => {
    if (get<HTMLButtonElement>('.node-upload').disabled) {
      event.preventDefault()
    }
  })
  const parameters = createActionMenu(get<HTMLDetailsElement>('.node-parameters'))
  const submit = get<HTMLButtonElement>('.node-submit')
  const status = get('.node-status')
  const upload = document.createElement('input')
  upload.type = 'file'
  upload.accept = 'image/png,image/jpeg,image/webp'
  let referenceOperation: AbortController | null = null
  let activeId: string | null = null
  let caps: Capabilities | null = null
  let expanded = false
  let needsReveal = false
  let frame = 0
  let lastSignature = ''
  let dialogTarget: string | null = null
  const uploading = new Set<string>()
  const submitting = new Set<string>()
  const errors = new Map<string, string>()

  function update(obj: CanvasObj, patch: Partial<CanvasObj>) {
    const before: Record<string, unknown> = {}
    for (const key of Object.keys(patch) as (keyof CanvasObj)[]) {
      before[key] = obj[key]
    }
    store.apply(cmdUpdateObject(obj.id, patch, before))
  }
  function updateDraft(obj: CanvasObj, patch: Partial<NodeDraft>) {
    if (obj.nodeDraft) {
      update(obj, { nodeDraft: { ...obj.nodeDraft, ...patch } })
    }
  }
  function current() {
    return activeId ? store.doc.objects[activeId] : undefined
  }
  function optionsFor(
    control: HTMLSelectElement,
    values: { value: string; label: string }[],
    value: string,
  ) {
    const signature = JSON.stringify(values)
    if (control.dataset.options !== signature) {
      control.replaceChildren(...values.map((item) => new Option(item.label, item.value)))
      control.dataset.options = signature
    }
    control.value = value
    dropdowns.get(control)?.sync()
  }
  async function loadCapabilities() {
    try {
      const response = await fetch('/api/generation-capabilities')
      if (!response.ok) {
        throw new Error('无法读取生成能力')
      }
      caps = (await response.json()) as Capabilities
      lastSignature = ''
      refresh()
    } catch {
      status.textContent = '模型信息暂不可用，请检查设置后重新选择节点'
      submit.disabled = true
    }
  }
  function defaults(obj: CanvasObj) {
    if (!isMediaNode(obj) || !caps || isBusy(obj)) {
      return
    }
    const cap = caps[obj.kind]
    const draft = obj.nodeDraft
    const next = { ...draft }
    if (!cap.models.some((m) => m.id === draft.model)) {
      next.model = cap.models[0]?.id || ''
    }
    if (!cap.ratios.includes(draft.ratio)) {
      next.ratio = cap.ratios[0]
    }
    if (!cap.resolutions.includes(draft.resolution)) {
      next.resolution = cap.resolutions[0]
    }
    if (JSON.stringify(next) !== JSON.stringify(draft)) {
      update(obj, { nodeDraft: next })
    }
  }
  function render(obj: CanvasObj) {
    if (!isMediaNode(obj)) {
      return
    }
    const cap = caps?.[obj.kind]
    const draft = obj.nodeDraft
    const busy = isBusy(obj)
    get('.node-parameters-value').textContent =
      `${draft.ratio} · ${draft.resolution >= 1024 && draft.resolution % 1024 === 0 ? `${draft.resolution / 1024}K` : `${draft.resolution}px`} · 1`
    panel.classList.toggle('is-expanded', expanded)
    panel.dataset.kind = obj.kind
    get('.node-panel-title').textContent =
      `${obj.name || (obj.kind === 'video' ? '视频' : '图片')} · ${hasOutput(obj) ? '继续创作' : '创建内容'}`
    prompt.placeholder =
      obj.kind === 'video'
        ? '上传首帧参考图，描述你想生成的视频、动作与镜头'
        : '上传参考图，描述你想生成的图片或修改要求'
    if (document.activeElement !== prompt) {
      prompt.value = draft.prompt
    }
    optionsFor(
      model,
      (cap?.models || []).map((m) => ({ value: m.id, label: m.name })),
      draft.model,
    )
    optionsFor(
      ratio,
      (cap?.ratios || []).map((value) => ({ value, label: value })),
      draft.ratio,
    )
    optionsFor(
      resolution,
      (cap?.resolutions || []).map((value) => ({ value: String(value), label: `${value}px` })),
      String(draft.resolution),
    )
    optionsFor(
      duration,
      (cap?.durations || []).map((value) => ({ value: String(value), label: `${value} 秒` })),
      String(draft.durationSec),
    )
    denoise.value = String(draft.denoise)
    get('.node-duration-wrap').hidden = obj.kind !== 'video'
    get('.node-denoise-wrap').hidden = !cap?.denoise || !draft.references.length
    get('.node-references').hidden = cap?.referenceLimit === 0
    const items = get('.node-reference-items')
    const signature = JSON.stringify(draft.references)
    if (items.dataset.signature !== signature) {
      items.replaceChildren(
        ...draft.references.map((ref) => {
          const chip = document.createElement('div')
          chip.className = 'node-reference'
          const img = document.createElement('img')
          img.src = `/assets/${ref.assetId}/original.${ref.ext}`
          img.alt = ref.name
          const remove = document.createElement('button')
          remove.type = 'button'
          remove.textContent = '移除'
          remove.setAttribute('aria-label', `移除参考图 ${ref.name}`)
          remove.addEventListener('click', () => {
            const target = current()
            if (target) {
              updateDraft(target, {
                references: target.nodeDraft!.references.filter(
                  (item) => item.assetId !== ref.assetId,
                ),
              })
            }
          })
          chip.append(img, remove)
          return chip
        }),
      )
      items.dataset.signature = signature
    }
    for (const control of Array.from(
      panel.querySelectorAll<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>(
        '.node-settings input,.node-settings select,.node-prompt,.node-canvas-reference',
      ),
    )) {
      control.disabled = busy
    }
    dropdowns.forEach((dropdown) => dropdown.sync())
    for (const button of Array.from(
      panel.querySelectorAll<HTMLButtonElement>('.node-reference-popup button'),
    )) {
      button.disabled = busy || uploading.has(obj.id)
    }
    for (const button of Array.from(items.querySelectorAll<HTMLButtonElement>('button'))) {
      button.disabled = busy
    }
    const invalidDuration =
      obj.kind === 'video' && !!cap?.durations?.length && !cap.durations.includes(draft.durationSec)
    const referenceOverflow = !!cap && draft.references.length > cap.referenceLimit
    submit.disabled =
      invalidDuration ||
      referenceOverflow ||
      !cap?.supported ||
      !draft.model ||
      !draft.prompt.trim() ||
      uploading.has(obj.id) ||
      (busy && obj.nodeRun?.status !== 'uncertain')
    submit.querySelector('span')!.textContent =
      obj.nodeRun?.status === 'uncertain'
        ? '确认结果'
        : busy
          ? '生成中'
          : obj.nodeRun?.status === 'failed'
            ? '重试'
            : hasOutput(obj)
              ? '生成新节点'
              : '生成'
    submit.title = submit.querySelector('span')!.textContent || '生成'
    get('.node-cancel').hidden = !busy
    get('.node-mode-hint').textContent =
      obj.kind === 'video'
        ? draft.references.length
          ? '首帧生视频 · 1 段'
          : '文字生视频 · 1 段'
        : draft.references.length
          ? '参考图生图 · 1 张'
          : hasOutput(obj) && cap?.referenceLimit
            ? '以当前图片为参考 · 1 张'
            : '文字生图 · 1 张'
    status.textContent =
      (invalidDuration
        ? `当前模型不支持 ${draft.durationSec} 秒，请选择可用视频时长；分镜计划时长保持不变`
        : '') ||
      (referenceOverflow
        ? `已选 ${draft.references.length} 张；当前模型最多支持 ${cap?.referenceLimit} 张参考图，请减少后生成`
        : '') ||
      errors.get(obj.id) ||
      (uploading.has(obj.id)
        ? '正在上传参考图…'
        : !cap
          ? '正在读取模型…'
          : !cap.supported
            ? cap.reason || '当前后端不支持'
            : obj.nodeRun?.message || '')
    status.classList.toggle('is-error', !!errors.get(obj.id) || obj.nodeRun?.status === 'failed')
  }
  function position(element: HTMLElement, obj: CanvasObj, above: boolean) {
    const surface = engine.stage.container().getBoundingClientRect() as DOMRect
    const point = engine.worldToScreen({ x: obj.x, y: obj.y })
    const scale = engine.getCam().scale
    const w = element.offsetWidth
    const h = element.offsetHeight
    const minY = Math.max(72, surface.top + 8)
    const maxY = Math.max(minY, Math.min(window.innerHeight - 12, surface.bottom) - h)
    const belowY = surface.top + point.y + obj.height * scale + 20
    const aboveY = surface.top + point.y - h - 24
    const preferred = above ? aboveY : belowY > maxY && aboveY >= minY ? aboveY : belowY
    const x = surface.left + point.x + (obj.width * scale) / 2 - w / 2
    element.style.left = `${Math.max(12, Math.min(Math.min(window.innerWidth, surface.right) - w - 12, x))}px`
    element.style.top = `${expanded && !above ? Math.max(minY, (window.innerHeight - h) / 2) : Math.max(minY, Math.min(maxY, preferred))}px`
  }
  function refresh() {
    const selection = getSelection()
    const obj = selection.length === 1 ? store.doc.objects[selection[0]] : undefined
    const blocked =
      !engine.interaction.canShowObjectActions() ||
      ['cutout-bar', 'inpaint-bar'].some((id) => {
        const el = document.getElementById(id)
        return el && !el.hidden
      }) ||
      !document.getElementById('text-editor')?.hidden
    if (blocked) {
      referenceMenu.close()
      parameters.close()
      dropdowns.forEach((dropdown) => dropdown.close())
      panel.hidden = true
      textBar.hidden = true
      return
    }
    if (!obj || ((obj.kind === 'image' || obj.kind === 'video') && hasOutput(obj))) {
      dropdowns.forEach((dropdown) => dropdown.close())
      panel.hidden = true
      textBar.hidden = true
      referenceMenu.close()
      parameters.close()
      referenceOperation?.abort()
      activeId = null
      lastSignature = ''
      return
    }
    if (activeId !== obj.id) {
      referenceMenu.close()
      parameters.close()
      referenceOperation?.abort()
      activeId = obj.id
      if (isEmptyMedia(obj)) {
        const scale = engine.getCam().scale
        const minimum = obj.kind === 'video' ? 232 : 132
        if (obj.width * scale < minimum) {
          const factor = minimum / (obj.width * scale)
          update(obj, {
            x: obj.x - (obj.width * (factor - 1)) / 2,
            width: obj.width * factor,
            height: obj.height * factor,
          })
        }
      }
      needsReveal = true
      expanded = false
      lastSignature = ''
    }
    defaults(obj)
    panel.hidden = !isEmptyMedia(obj)
    textBar.hidden = obj.kind !== 'text'
    if (!panel.hidden) {
      panel.style.width = `${Math.max(240, Math.min(expanded ? 960 : 680, engine.stage.container().getBoundingClientRect().width - 24))}px`
      const signature = JSON.stringify([
        obj.nodeDraft,
        obj.nodeRun,
        obj.src,
        obj.assetId,
        obj.name,
        expanded,
        uploading.has(obj.id),
        errors.get(obj.id),
        caps,
        store.doc.order,
      ])
      if (signature !== lastSignature) {
        render(obj)
        lastSignature = signature
      }
      needsReveal = false
      position(panel, obj, false)
      const actions = document.getElementById('obj-actions')
      if (actions) {
        actions.hidden = true
      }
    }
    if (!textBar.hidden) {
      if (needsReveal) {
        needsReveal = false
        const surface = engine.stage.container().getBoundingClientRect() as DOMRect
        const point = engine.worldToScreen({ x: obj.x, y: obj.y })
        const scale = engine.getCam().scale
        const dx =
          point.x < 64 || point.x + obj.width * scale > surface.width - 20
            ? surface.width / 2 - point.x - (obj.width * scale) / 2
            : 0
        const dy =
          point.y < 120
            ? 120 - point.y
            : point.y + obj.height * scale > surface.height - 24
              ? surface.height - 24 - point.y - obj.height * scale
              : 0
        engine.panBy(dx, dy)
      }
      textBar.querySelector<HTMLInputElement>('input')!.value =
        obj.backgroundColor ||
        (document.documentElement.dataset.theme === 'dark' ? '#25262c' : '#ffffff')
      position(textBar, obj, true)
    }
  }
  function schedule() {
    if (!frame) {
      frame = requestAnimationFrame(() => {
        frame = 0
        refresh()
      })
    }
  }
  prompt.addEventListener('input', () => {
    const obj = current()
    if (obj) {
      updateDraft(obj, { prompt: prompt.value })
    }
  })
  for (const [control, field] of [
    [model, 'model'],
    [ratio, 'ratio'],
    [resolution, 'resolution'],
    [duration, 'durationSec'],
    [denoise, 'denoise'],
  ] as const) {
    control.addEventListener('change', () => {
      const obj = current()
      if (!obj) {
        return
      }
      updateDraft(obj, {
        [field]: ['model', 'ratio'].includes(field) ? control.value : Number(control.value),
      })
    })
  }
  async function attachReference(obj: CanvasObj, ref: Reference) {
    if (store.doc.objects[obj.id] !== obj || isBusy(obj)) {
      return
    }
    const refs = obj.nodeDraft?.references || []
    if (!refs.some((item) => item.assetId === ref.assetId)) {
      updateDraft(obj, { references: [...refs, ref] })
    }
  }
  function startReferenceOperation() {
    referenceOperation?.abort()
    referenceOperation = new AbortController()
    referenceMenu.close()
    return referenceOperation
  }
  get('.node-pick-asset').addEventListener('click', async () => {
    const target = current()
    if (!target) {
      return
    }
    const operation = startReferenceOperation()
    const ref = await pickReferenceAsset(operation.signal)
    if (ref && !operation.signal.aborted && current() === target) {
      await attachReference(target, ref)
    }
  })
  get('.node-pick-canvas').addEventListener('click', () => {
    const target = current()
    if (!target) {
      return
    }
    const operation = startReferenceOperation()
    const surface = engine.stage.container()
    panel.classList.add('is-picking-reference')
    const banner = document.createElement('div')
    banner.className = 'reference-pick-banner'
    banner.innerHTML =
      '<span role="status">连续点击图片添加参考，再次点击取消选中</span><button type="button">退出选择 · Esc</button>'
    document.body.append(banner)
    const overlays = document.createElement('div')
    overlays.className = 'reference-pick-overlays'
    document.body.append(overlays)
    let hovered: string | null = null
    let paintFrame = 0
    const markers = new Map<string, HTMLElement>()
    const badges = new Map<string, HTMLElement>()
    const pendingSources = new Set<string>()
    const validSource = (id: string | null) => {
      const source = id ? store.doc.objects[id] : undefined
      return source && source !== target && source.kind === 'image' && hasOutput(source)
        ? source
        : null
    }
    const selected = (source: CanvasObj) =>
      target.nodeDraft!.references.some(
        (ref) =>
          ref.sourceNodeId === source.id || (!!source.assetId && ref.assetId === source.assetId),
      )
    function paint() {
      if (operation.signal.aborted) {
        return
      }
      const rect = surface.getBoundingClientRect()
      const scale = engine.getCam().scale
      const visible = new Set<string>()
      for (const source of Object.values(store.doc.objects)) {
        if (
          !validSource(source.id) ||
          (!selected(source) && source.id !== hovered && !pendingSources.has(source.id))
        ) {
          continue
        }
        visible.add(source.id)
        let marker = markers.get(source.id)
        if (!marker) {
          marker = document.createElement('div')
          marker.className = 'reference-pick-outline'
          const badge = document.createElement('span')
          badge.className = 'reference-pick-badge'
          badges.set(source.id, badge)
          overlays.append(marker, badge)
          markers.set(source.id, marker)
        }
        const point = engine.worldToScreen({ x: source.x, y: source.y })
        Object.assign(marker.style, {
          left: `${rect.left + point.x}px`,
          top: `${rect.top + point.y}px`,
          width: `${source.width * scale}px`,
          height: `${source.height * scale}px`,
          transform: `rotate(${source.rotation || 0}deg)`,
        })
        marker.classList.toggle('is-selected', selected(source))
        marker.classList.toggle('is-hovered', source.id === hovered)
        const badge = badges.get(source.id)!
        badge.textContent = pendingSources.has(source.id)
          ? '添加中…'
          : selected(source)
            ? '已选 · 点击移除'
            : '点击添加'
        // 提示属于屏幕覆盖层，不能随图片缩放、旋转或移出可见区域。
        const bounds = marker.getBoundingClientRect()
        const visibleTop = Math.max(rect.top, 64)
        const visibleBottom = Math.min(rect.bottom, window.innerHeight)
        badge.hidden =
          bounds.bottom <= visibleTop ||
          bounds.top >= visibleBottom ||
          bounds.right <= rect.left ||
          bounds.left >= rect.right
        badge.style.left = `${Math.max(rect.left + 8, Math.min(bounds.left + 8, rect.right - badge.offsetWidth - 8))}px`
        badge.style.top = `${Math.max(visibleTop + 8, Math.min(bounds.top + 8, visibleBottom - badge.offsetHeight - 8))}px`
      }
      for (const [id, marker] of markers) {
        if (!visible.has(id)) {
          marker.remove()
          badges.get(id)?.remove()
          badges.delete(id)
          markers.delete(id)
        }
      }
      paintFrame = requestAnimationFrame(paint)
    }
    paint()
    const hover = (event: PointerEvent) => {
      const rect = surface.getBoundingClientRect()
      const id = options.pickObject(
        engine.screenToWorld({ x: event.clientX - rect.left, y: event.clientY - rect.top }),
      )
      hovered = validSource(id)?.id || null
      surface.style.cursor = hovered ? 'pointer' : 'crosshair'
    }
    const leave = () => {
      hovered = null
    }
    surface.addEventListener('pointermove', hover)
    surface.addEventListener('pointerleave', leave)
    const previousCursor = surface.style.cursor
    surface.style.cursor = 'crosshair'
    const cancel = () => operation.abort()
    banner.querySelector('button')!.onclick = cancel
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopImmediatePropagation()
        cancel()
      }
    }
    const intercept = (event: PointerEvent) => {
      if (!(event.target instanceof Node) || !surface.contains(event.target)) {
        return
      }
      event.preventDefault()
      event.stopImmediatePropagation()
      if (event.type !== 'click' || event.button !== 0) {
        return
      }
      const rect = surface.getBoundingClientRect()
      const id = options.pickObject(
        engine.screenToWorld({ x: event.clientX - rect.left, y: event.clientY - rect.top }),
      )
      const source = id ? store.doc.objects[id] : undefined
      if (!source || source === target || source.kind !== 'image' || !hasOutput(source)) {
        banner.querySelector('span')!.textContent = '请选择已有图片，不能选择空节点或视频'
        return
      }
      if (pendingSources.has(source.id)) {
        return
      }
      if (selected(source)) {
        updateDraft(target, {
          references: target.nodeDraft!.references.filter(
            (ref) =>
              ref.sourceNodeId !== source.id && (!source.assetId || ref.assetId !== source.assetId),
          ),
        })
        return
      }
      pendingSources.add(source.id)
      uploading.add(target.id)
      schedule()
      void ensureAsset(source, operation.signal)
        .then(async (asset) => {
          if (
            !operation.signal.aborted &&
            current() === target &&
            store.doc.objects[source.id] === source
          ) {
            await attachReference(target, {
              assetId: asset.id,
              ext: asset.ext,
              name: source.name || '画布图片',
              sourceNodeId: source.id,
            })
          }
        })
        .catch((error) => {
          if (!operation.signal.aborted) {
            notify(error instanceof Error ? error.message : '参考图读取失败')
          }
        })
        .finally(() => {
          pendingSources.delete(source.id)
          if (!pendingSources.size) {
            uploading.delete(target.id)
          }
          if (!operation.signal.aborted) {
            banner.querySelector('span')!.textContent =
              `已选 ${target.nodeDraft!.references.length} 张，继续点击添加或移除；退出后保留已选图片`
          }
          schedule()
        })
    }
    for (const type of ['pointerdown', 'pointerup', 'click', 'dblclick']) {
      document.addEventListener(type, intercept as EventListener, true)
    }
    document.addEventListener('keydown', key, true)
    operation.signal.addEventListener(
      'abort',
      () => {
        banner.remove()
        panel.classList.remove('is-picking-reference')
        cancelAnimationFrame(paintFrame)
        overlays.remove()
        surface.removeEventListener('pointermove', hover)
        surface.removeEventListener('pointerleave', leave)
        surface.style.cursor = previousCursor
        for (const type of ['pointerdown', 'pointerup', 'click', 'dblclick']) {
          document.removeEventListener(type, intercept as EventListener, true)
        }
        document.removeEventListener('keydown', key, true)
      },
      { once: true },
    )
  })
  get('.node-upload').addEventListener('click', () => {
    referenceMenu.close()
    upload.value = ''
    upload.click()
  })
  upload.addEventListener('change', async () => {
    const obj = current(),
      file = upload.files?.[0]
    if (!obj || !file) {
      return
    }
    if (
      file.size > 8 * 1024 * 1024 ||
      !['image/png', 'image/jpeg', 'image/webp'].includes(file.type)
    ) {
      errors.set(obj.id, '请选择不超过 8MB 的 PNG、JPEG 或 WebP 图片')
      lastSignature = ''
      schedule()
      return
    }
    uploading.add(obj.id)
    errors.delete(obj.id)
    schedule()
    try {
      const ext = file.type.split('/')[1]
      const response = await fetch(`/api/assets?ext=${ext}`, {
        method: 'POST',
        headers: { 'Content-Type': file.type },
        body: file,
      })
      const body = (await response.json()) as { asset: { id: string; ext: string }; error?: string }
      if (!response.ok) {
        throw new Error(body.error || '上传失败')
      }
      await attachReference(obj, { assetId: body.asset.id, ext: body.asset.ext, name: file.name })
    } catch (error) {
      errors.set(obj.id, error instanceof Error ? error.message : '上传失败')
    } finally {
      uploading.delete(obj.id)
      schedule()
    }
  })
  submit.addEventListener('click', async () => {
    let obj = current()
    if (!obj || !isMediaNode(obj) || submit.disabled) {
      return
    }
    if (submitting.has(obj.id)) {
      return
    }
    const submissionId = obj.id
    submitting.add(submissionId)
    try {
      if (hasOutput(obj)) {
        const source = obj
        const next = createNode(
          obj.kind,
          { x: obj.x + obj.width + 48, y: obj.y },
          obj.kind === 'image' ? '图片变体' : '视频变体',
        )
        next.nodeDraft = structuredClone(obj.nodeDraft)
        try {
          if (
            obj.kind === 'image' &&
            !next.nodeDraft.references.length &&
            caps?.image.referenceLimit
          ) {
            const asset = await ensureAsset(source)
            next.nodeDraft.references = [
              {
                assetId: asset.id,
                ext: asset.ext,
                name: source.name || '原图',
                sourceNodeId: source.id,
              },
            ]
          }
        } catch {
          notify('原图读取失败，请重试')
          return
        }
        if (store.doc.objects[source.id] !== source) {
          return
        }
        store.apply(cmdAddObjects([next]))
        select(next.id)
        obj = next
      }
      errors.delete(obj.id)
      await generation.submit(obj)
    } finally {
      submitting.delete(submissionId)
      schedule()
    }
  })
  prompt.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault()
      e.stopPropagation()
      submit.click()
    }
  })
  get('.node-cancel').addEventListener('click', () => {
    const obj = current()
    if (obj) {
      generation.cancel(obj)
    }
  })
  get('.node-expand').addEventListener('click', () => {
    expanded = !expanded
    lastSignature = ''
    refresh()
    prompt.focus()
  })
  function download(url: string, name: string) {
    const a = document.createElement('a')
    a.href = url
    a.download = name
    a.click()
  }
  textBar.querySelector('input')!.addEventListener('input', (e) => {
    const obj = current()
    if (obj) {
      update(obj, { textCard: true, backgroundColor: (e.target as HTMLInputElement).value })
    }
  })
  textBar.querySelector('.node-text-edit')!.addEventListener('click', () => {
    if (activeId) {
      editText(activeId)
      schedule()
    }
  })
  textBar.querySelector('.node-text-expand')!.addEventListener('click', () => {
    const obj = current()
    if (!obj) {
      return
    }
    dialogTarget = obj.id
    dialog.querySelector('textarea')!.value = obj.text || ''
    dialog.returnValue = ''
    dialog.showModal()
  })
  dialog.addEventListener('close', () => {
    const obj = dialogTarget ? store.doc.objects[dialogTarget] : undefined
    if (dialog.returnValue === 'save' && obj) {
      update(obj, { text: dialog.querySelector('textarea')!.value })
    }
    dialogTarget = null
  })
  textBar.querySelector('.node-text-download')!.addEventListener('click', () => {
    const obj = current()
    if (!obj) {
      return
    }
    const url = URL.createObjectURL(
      new Blob([obj.text || ''], { type: 'text/plain;charset=utf-8' }),
    )
    download(url, `${obj.name || '文本'}.txt`)
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  })
  const unstore = store.subscribe(schedule)
  const unmode = engine.interaction.subscribe(schedule)
  const observed = new MutationObserver(schedule)
  for (const id of ['text-editor', 'inpaint-bar', 'cutout-bar', 'chat-panel']) {
    const el = document.getElementById(id)
    if (el) {
      observed.observe(el, { attributes: true, attributeFilter: ['hidden', 'class', 'style'] })
    }
  }
  const selectionChange = () => schedule()
  document.addEventListener('gencanvas:selectionchange', selectionChange)
  document.addEventListener('gencanvas:themechange', schedule)
  document.addEventListener('gencanvas:maskmode', schedule)
  window.addEventListener('resize', schedule)
  const settings = document.getElementById('settings-dialog')
  settings?.addEventListener('sl-after-hide', loadCapabilities)
  void loadCapabilities()
  async function prepareNode(id: string, signal?: AbortSignal) {
    const obj = store.doc.objects[id]
    if (!obj || !isMediaNode(obj) || hasOutput(obj)) {
      throw new Error('请先为镜头创建新的生成节点')
    }
    const original = obj.nodeDraft
    if (!caps) {
      await loadCapabilities()
    }
    if (signal?.aborted || store.doc.objects[id] !== obj || obj.nodeDraft !== original) {
      throw new Error('节点已修改或准备已取消，请重新操作')
    }
    if (isBusy(obj) || obj.nodeRun?.status === 'uncertain') {
      throw new Error('节点已有任务，请先处理当前任务')
    }
    const draft = prepareNodeDraft(obj, caps?.[obj.kind])
    if (JSON.stringify(draft) !== JSON.stringify(obj.nodeDraft)) {
      update(obj, { nodeDraft: draft })
    }
    return structuredClone(draft)
  }
  return {
    prepareNode,
    refresh: schedule,
    async submitNode(id: string, onSubmitted?: (requestId: string) => void, signal?: AbortSignal) {
      const obj = store.doc.objects[id]
      if (!obj || !isMediaNode(obj) || hasOutput(obj)) {
        throw new Error('请先为镜头创建新的生成节点')
      }
      if (signal?.aborted) {
        throw new Error('已取消提交')
      }
      if (obj.nodeRun?.status === 'uncertain') {
        await generation.submit(obj, onSubmitted)
        return
      }
      if (isBusy(obj)) {
        return
      }
      const prepared = await prepareNode(id, signal)
      if (
        signal?.aborted ||
        store.doc.objects[id] !== obj ||
        JSON.stringify(obj.nodeDraft) !== JSON.stringify(prepared)
      ) {
        throw new Error('节点已修改或准备已取消，请重新操作')
      }
      if (isBusy(obj)) {
        return
      }
      await generation.submit(obj, onSubmitted)
    },
    cancelNode(id: string) {
      const obj = store.doc.objects[id]
      if (obj) {
        generation.cancel(obj)
      }
    },
    handleReferenceKey(event: KeyboardEvent) {
      if (!panel.classList.contains('is-picking-reference')) {
        return false
      }
      if (event.key === 'Escape') {
        referenceOperation?.abort()
      }
      event.preventDefault()
      return true
    },
    dispose() {
      referenceOperation?.abort()
      referenceMenu.dispose()
      parameters.dispose()
      dropdowns.forEach((dropdown) => dropdown.dispose())
      generation.dispose()
      unstore()
      unmode()
      observed.disconnect()
      panel.remove()
      textBar.remove()
      dialog.remove()
      window.removeEventListener('resize', schedule)
    },
  }
}
