// 右侧对话栏(PRD 约束 3 / §5.2 / §7):消息、任务卡与结果同处一栏。 
// 消息列表持久化在文档 chat 字段(随文档保存/恢复,PRD A02 所属会话重开一致);
// 组卡不存快照,渲染时从画布对象实时推导四槽位状态(刷新后自愈)。 
// 用户内容一律 textContent 注入,不经 innerHTML。 

import { groupCardState } from '../state/group-state.js'

/** @param {string} prefix */
function localId(prefix) {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
}

/**
 * @param {{
 *   docStore: { doc: any, mutateTransient: (fn: (doc: any) => unknown) => unknown, subscribe: (fn: any) => void },
 *   container: HTMLElement,
 *   actions: {
 *     focusObject?: (objId: string) => void,
 *     openMask?: (objId: string, maskUrl?: string, sessionId?: string) => void,
 *     regenerateGroup?: (groupId: string) => void,
 *     confirmGroup?: (groupId: string) => void,
 *     placeResult?: (info: { assetId: string, ext: string, width: number, height: number, sessionId?: string, hasThumbs?: boolean }) => void,
 *     addToConversation?: (objId: string) => void,
 *   },
 * }} options
 */
export function createChat({ docStore, container, actions = {} }) {
  const doc = () => docStore.doc
  if (!Array.isArray(doc().chat)) {
    doc().chat = []
  }
  if (!doc().groups || typeof doc().groups !== 'object') {
    doc().groups = {}
  }

  /** 追加消息(持久化)并渲染。 @param {any} entry @returns {string} id */
  function push(entry) {
    const id = entry.id || localId('msg')
    docStore.mutateTransient(() => {
      doc().chat.push({ id, at: new Date().toISOString(), ...entry })
      // 上限防膨胀:服务端同样限制 500 条
      if (doc().chat.length > 500) {
        doc().chat.splice(0, doc().chat.length - 500)
      }
    })
    render()
    return id
  }

  /** @param {string} text @param {string} [id] */
  const addUser = (text, id) => push({ id, role: 'user', kind: 'text', text })
  /** @param {string} text @param {string} [id] */
  const addAssistant = (text, id) => push({ id, role: 'assistant', kind: 'text', text })
  /** @param {string} text */
  const addError = (text) => push({ role: 'assistant', kind: 'text', error: true, text })
  /** @param {string} text */
  const addSystem = (text) => push({ role: 'system', kind: 'text', text })

  /** 组卡(PRD §5.2:对话同组预览)。 @param {string} groupId @param {string[]} slotObjIds @param {string} prompt */
  const addGroupCard = (groupId, slotObjIds, prompt) =>
    push({ role: 'system', kind: 'group', groupId, slotObjIds, prompt })

  /** Agent 计划卡(PRD §7)。 @param {string} sessionId @param {any} plan @param {string} request @param {string} [assetThumb] */
  const addPlanCard = (sessionId, plan, request, assetThumb) =>
    push({ role: 'assistant', kind: 'plan', sessionId, plan, request, assetThumb })

  /** 会话状态更新(等待蒙版 / 完成):更新同会话现有消息而非追加。 @param {string} sessionId @param {any} patch */
  function updateSession(sessionId, patch) {
    docStore.mutateTransient(() => {
      for (const entry of doc().chat) {
        if (entry.sessionId === sessionId) {
          Object.assign(entry, patch)
        }
      }
    })
    render()
  }

  /**
   * 抠图结果卡(PRD §6:对话展示预览和"来自图片 N")。
   * pendingPlace:资产已生成但占位对象已不在画布(被删除)→ 提供"放入画布"。
   * @param {{ objId?: string, assetUrl?: string, sourceLabel?: string, sessionId?: string,
   *           pendingPlace?: boolean, assetId?: string, ext?: string, width?: number, height?: number,
   *           hasThumbs?: boolean }} options
   */
  const addResultCard = (options) =>
    push({
      role: 'assistant',
      kind: 'result',
      objId: options.objId,
      assetUrl: options.assetUrl,
      sourceLabel: options.sourceLabel,
      sessionId: options.sessionId,
      pendingPlace: options.pendingPlace,
      assetId: options.assetId,
      ext: options.ext,
      width: options.width,
      height: options.height,
      hasThumbs: options.hasThumbs,
    })

  /** 放置完成:更新同一结果卡为已落位。 @param {string} sessionId @param {string} objId */
  const markPlaced = (sessionId, objId) => {
    docStore.mutateTransient(() => {
      for (const entry of doc().chat) {
        if (entry.sessionId === sessionId && entry.kind === 'result') {
          entry.pendingPlace = false
          entry.objId = objId
        }
      }
    })
    render()
  }

  /* ---------- 渲染 ---------- */

  /**
   * DOM 快捷构造;内容一律 textContent。
   * @param {keyof HTMLElementTagNameMap} tag
   * @param {string | null} [className]
   * @param {string} [text]
   */
  const el = (tag, className, text) => {
    const node = document.createElement(tag)
    if (className) {
      node.className = className
    }
    if (text != null) {
      node.textContent = text
    }
    return node
  }

  /** 组卡实时状态:从画布对象推导(placeholder/image/error)。 @param {any} entry */
  function groupLiveState(entry) {
    const slots = (entry.slotObjIds || [])
      .map((/** @type {string} */ id) => doc().objects[id])
      .filter(Boolean)
    const pending = slots.filter((/** @type {any} */ o) => o.kind === 'placeholder')
    let state = { key: 'running', label: pending[0]?.gen?.message || '生成中…' }
    if (pending.some((/** @type {any} */ o) => o.pendingConfirm)) {
      // 提交未收到确认:等待用户显式查询(A06),不显示进度也不提供重试
      state = { key: 'pending', label: '结果待确认' }
    } else if (pending.length === 0) {
      const fake = slots.map((/** @type {any} */ o) => ({
        status: o.kind === 'image' ? 'completed' : 'failed',
      }))
      state = groupCardState(fake)
    }
    return { slots, pending, state }
  }

  /** @param {any} entry */
  function buildGroupCard(entry) {
    const card = el('div', 'chat-card')
    const { slots, state } = groupLiveState(entry)
    const title = el('div', 'chat-card-title')
    const groupMeta = doc().groups?.[entry.groupId]
    title.append(el('span', null, groupMeta?.title || '四方向生成'))
    const stateEl = el(
      'span',
      `chat-card-state ${state.key === 'done' ? 'done' : state.key === 'failed' ? 'fail' : ''}`,
      state.label,
    )
    title.append(stateEl)
    card.append(title)

    const grid = el('div', 'group-grid')
    ;(entry.slotObjIds || []).forEach((/** @type {string} */ objId, /** @type {number} */ index) => {
      const cell = el('div', 'group-cell')
      cell.append(el('span', 'cell-slot', String(index + 1)))
      const obj = doc().objects[objId]
      if (!obj) {
        cell.append(el('span', 'cell-state', '已移除'))
      } else if (obj.kind === 'image' || obj.kind === 'video') {
        const url = obj.assetId
          ? `/assets/${obj.assetId}/${obj.hasThumbs === false ? `original.${obj.ext || 'png'}` : 't256.webp'}`
          : obj.src
        if (url) {
          const img = document.createElement('img')
          img.src = url
          img.alt = `方向 ${index + 1}`
          img.loading = 'lazy'
          img.addEventListener('click', () => actions.focusObject?.(objId))
          cell.append(img)
        }
      } else if (obj.kind === 'error') {
        cell.classList.add('fail')
        cell.append(el('span', null, obj.errorDetail || '生成失败'))
      } else if (obj.pendingConfirm) {
        cell.append(el('span', 'cell-state', '结果待确认'))
      } else {
        cell.append(el('span', 'cell-state', obj.gen?.message || '准备中'))
      }
      grid.append(cell)
    })
    card.append(grid)

    const prompt = el('div', 'chat-card-prompt', entry.prompt || groupMeta?.prompt || '')
    prompt.title = entry.prompt || ''
    card.append(prompt)

    // 任务详情展开(§5.1):完整需求 + 四条方向(标题/维度/提示词全文)
    if (entry.expanded && Array.isArray(groupMeta?.directions)) {
      const detail = el('div', 'chat-card-detail')
      for (const dir of groupMeta.directions) {
        const row = el('div', 'detail-direction')
        row.append(el('b', null, `${dir.slot + 1}. ${dir.title} · ${dir.dimension}`))
        row.append(el('p', null, dir.prompt))
        detail.append(row)
      }
      card.append(detail)
    }

    if (slots.length > 0) {
      const actionsRow = el('div', 'chat-card-actions')
      if (state.key === 'pending') {
        const check = el('button', 'chat-mini-btn', '查询结果')
        check.title = '只查询服务端是否已接受本次请求,不会重复提交'
        check.addEventListener('click', () => actions.confirmGroup?.(entry.groupId))
        actionsRow.append(check)
      }
      if (state.key === 'partial' || state.key === 'failed') {
        const again = el('button', 'chat-mini-btn', '另生成一组 4 张')
        again.title = '将发起一次新的生成,可能产生新的调用费用'
        again.addEventListener('click', () => actions.regenerateGroup?.(entry.groupId))
        actionsRow.append(again)
      }
      if (state.key === 'done' || state.key === 'partial') {
        const view = el('button', 'chat-mini-btn', '在画布中查看')
        view.addEventListener('click', () => actions.focusObject?.(entry.slotObjIds[0]))
        actionsRow.append(view)
      }
      if (Array.isArray(groupMeta?.directions)) {
        const toggle = el('button', 'chat-mini-btn', entry.expanded ? '收起详情' : '任务详情')
        toggle.addEventListener('click', () => {
          docStore.mutateTransient(() => {
            entry.expanded = !entry.expanded
          })
          render()
        })
        actionsRow.append(toggle)
      }
      if (actionsRow.childElementCount > 0) {
        card.append(actionsRow)
      }
    }
    return card
  }

  /** @param {any} entry */
  function buildPlanCard(entry) {
    const card = el('div', 'chat-card')
    card.append(el('div', 'chat-card-title', '创作助手 · 执行计划'))
    const steps = el('ul', 'plan-steps')
    ;(entry.plan?.steps || []).forEach((/** @type {any} */ step, /** @type {number} */ index) => {
      const li = el('li')
      li.append(el('span', 'step-no', `${index + 1}.`), el('span', null, step.label))
      steps.append(li)
    })
    card.append(steps)
    card.append(
      el('div', 'plan-meta', '源图 1 张 · 输出 1 张 · 需要你确认蒙版'),
    )
    // 等待蒙版:提供"检查主体选区"入口(仅定位,不绕过抠图按钮,PRD §7)
    if (entry.waitingMask && entry.objId) {
      const actionsRow = el('div', 'chat-card-actions')
      const check = el('button', 'chat-mini-btn', '检查主体选区')
      check.addEventListener('click', () => actions.openMask?.(entry.objId, entry.maskUrl, entry.sessionId))
      actionsRow.append(check)
      card.append(actionsRow)
      card.append(el('div', 'plan-meta', '请在画布检查蒙版,点击蒙版栏"抠图"继续。 '))
    }
    return card
  }

  /** @param {any} entry */
  function buildResultCard(entry) {
    const card = el('div', 'chat-card')
    card.append(el('div', 'chat-card-title', entry.pendingPlace ? '结果已生成,尚未加入画布' : '抠图完成'))
    const row = el('div', 'result-preview')
    if (entry.assetUrl) {
      const img = document.createElement('img')
      img.src = entry.assetUrl
      img.alt = '透明素材预览'
      img.addEventListener('click', () => actions.focusObject?.(entry.objId))
      row.append(img)
    }
    const meta = el('div', 'result-source')
    meta.append(el('div', null, `来自 ${entry.sourceLabel || '引用图片'}`))
    if (entry.pendingPlace) {
      // 源对象被删除/撤销:资产保留,由用户显式放置(PRD §6,不复活已删对象)
      const hint = el('div', null, '原对象已不在画布;资产已保留,可显式放入。')
      meta.append(hint)
      const place = el('button', 'chat-mini-btn', '放入画布')
      place.addEventListener('click', () =>
        actions.placeResult?.({
          assetId: entry.assetId,
          ext: entry.ext,
          width: entry.width,
          height: entry.height,
          sessionId: entry.sessionId,
          hasThumbs: entry.hasThumbs,
        }),
      )
      meta.append(place)
    } else if (entry.objId) {
      const locate = el('button', 'chat-mini-btn', '定位到画布')
      locate.addEventListener('click', () => actions.focusObject?.(entry.objId))
      meta.append(locate)
    }
    row.append(meta)
    card.append(row)
    return card
  }

  /** @param {any} entry */
  function buildEntry(entry) {
    if (entry.kind === 'group') {
      return buildGroupCard(entry)
    }
    if (entry.kind === 'plan') {
      return buildPlanCard(entry)
    }
    if (entry.kind === 'result') {
      return buildResultCard(entry)
    }
    const wrap = el('div', `chat-msg ${entry.role}${entry.error ? ' error' : ''}`)
    wrap.append(el('div', 'bubble', entry.text || ''))
    return wrap
  }

  let renderQueued = false
  function render() {
    if (renderQueued) {
      return
    }
    renderQueued = true
    // 用微任务而不是 rAF 合并同帧多次渲染:后台/隐藏标签页里 rAF 永不触发,
    // 会导致消息渲染"卡住"(提交后组卡不出现、状态不刷新)。
    queueMicrotask(() => {
      renderQueued = false
      renderNow()
    })
  }

  function renderNow() {
    const messages = doc().chat || []
    container.replaceChildren()
    if (messages.length === 0) {
      container.append(
        el(
          'div',
          'chat-empty',
          '在下方输入需求开始创作;生成任务与结果会出现在这里。 ',
        ),
      )
      return
    }
    const nearBottom =
      container.scrollHeight - container.scrollTop - container.clientHeight < 120
    for (const entry of messages) {
      try {
        container.append(buildEntry(entry))
      } catch (err) {
        // 单条消息渲染失败不拖垮整栏,但要可见地暴露出来(便于定位)
        console.error('chat 消息渲染失败', entry?.kind, err)
        const bubble = el('div', 'chat-msg system')
        bubble.append(
          el(
            'div',
            'bubble',
            `[消息渲染失败:${err instanceof Error ? err.message : String(err)}]`,
          ),
        )
        container.append(bubble)
      }
    }
    if (nearBottom) {
      container.scrollTop = container.scrollHeight
    }
  }

  // 画布对象状态变化 → 组卡实时刷新(进度/落位/失败)
  docStore.subscribe(() => render())

  renderNow()

  return {
    push,
    addUser,
    addAssistant,
    addError,
    addSystem,
    addGroupCard,
    addPlanCard,
    updateSession,
    addResultCard,
    markPlaced,
    render,
  }
}
