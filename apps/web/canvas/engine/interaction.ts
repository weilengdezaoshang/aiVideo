export type Mode = 'select' | 'pan' | 'recognizing' | 'mask'
export type InteractionState = {
  mode: Mode
  effective: Mode
  targetId: string | null
  gesture: Mode | null
}
export type MaskHandlers = {
  beforeDown?: (e: PointerEvent) => void
  down: (e: PointerEvent) => void
  move: (e: PointerEvent) => void
  up: () => void
  cancel: () => void
  key: (e: KeyboardEvent) => void
}
/** @typedef {'select' | 'pan' | 'recognizing' | 'mask'} Mode */
/** @typedef {{mode: Mode, effective: Mode, targetId: string | null, gesture: Mode | null}} InteractionState */

/** 一份状态：临时平移不改变基础工具，按下时的归属保持到手势结束。 */
export function createInteraction() {
  /** @type {Mode} */
  let mode: Mode = 'select'
  /** @type {string | null} */
  let targetId: string | null = null
  /** @type {Mode | null} */
  let gesture: Mode | null = null
  let space = false
  const listeners = new Set<(state: InteractionState) => void>()
  const cancellations = new Set<() => void>()
  /** @returns {InteractionState} */
  const getState = (): InteractionState => ({
    mode,
    targetId,
    gesture,
    effective: gesture ?? (['select', 'recognizing'].includes(mode) && space ? 'pan' : mode),
  })
  const emit = () => {
    for (const fn of listeners) {
      fn(getState())
    }
  }
  function cancel() {
    for (const fn of cancellations) {
      fn()
    }
    gesture = null
    space = false
    emit()
  }
  return {
    getState,
    /** @param {(state: InteractionState) => void} fn */
    subscribe(fn: (state: InteractionState) => void) {
      listeners.add(fn)
      fn(getState())
      return () => {
        listeners.delete(fn)
      }
    },
    /** @param {() => void} fn */
    onCancel(fn: () => void) {
      cancellations.add(fn)
      return () => {
        cancellations.delete(fn)
      }
    },
    /** @param {Mode} next @param {string | null} [target] */
    setMode(next: Mode, target: string | null = null) {
      cancel()
      mode = next
      targetId = target
      emit()
    },
    /** @param {boolean} held */
    setSpace(held: boolean) {
      space = held
      emit()
    },
    /** @param {number} button @returns {Mode | null} */
    begin(button: number) {
      if (gesture || (button !== 0 && button !== 1)) {
        return null
      }
      if (mode === 'recognizing' && button !== 1 && !space) {
        return null
      }
      if (mode === 'mask' && button !== 0) {
        return null
      }
      gesture = button === 1 && mode !== 'mask' ? 'pan' : getState().effective
      emit()
      return gesture
    },
    end() {
      gesture = null
      emit()
    },
    cancel,
    canEdit: () => getState().effective === 'select',
    canNavigate: () => ['select', 'pan', 'recognizing'].includes(getState().effective),
    // 菜单展示与编辑权限分开：平移工具空闲时仍可展示，任何手势期间都隐藏。
    canShowObjectActions: () => !gesture && ['select', 'pan'].includes(getState().effective),
  }
}

/** @typedef {{beforeDown?:(e:PointerEvent)=>void, down:(e:PointerEvent)=>void, move:(e:PointerEvent)=>void, up:()=>void, cancel:()=>void, key:(e:KeyboardEvent)=>void}} MaskHandlers */

/** 输入框(包括 Web Component 内部输入)拥有自己的快捷键。 @param {Event} event */
export function isEditingInput(event: Event) {
  return event
    .composedPath()
    .some(
      (node) =>
        node instanceof HTMLElement &&
        (node.isContentEditable ||
          Boolean(
            node.matches('input, textarea, select, [contenteditable], sl-input, sl-textarea'),
          )),
    )
}

/** DOM 捕获入口；普通选择继续使用 Konva，平移/蒙版事件不再传入 Konva。
 * @param {HTMLElement} surface
 * @param {ReturnType<typeof createInteraction>} interaction
 * @param {{pan: (dx:number,dy:number)=>void, wheel:(event:WheelEvent)=>void}} camera
 */
export function bindInteraction(
  surface: HTMLElement,
  interaction: ReturnType<typeof createInteraction>,
  camera: { pan: (dx: number, dy: number) => void; wheel: (event: WheelEvent) => void },
) {
  /** @type {MaskHandlers | null} */
  let mask: MaskHandlers | null = null
  /** @type {(e:KeyboardEvent)=>void} */
  let selectionKey: (e: KeyboardEvent) => void = () => {}
  /** @type {() => void} */
  let cancelRecognition: () => void = () => {}
  let recognitionDown: ((event: PointerEvent) => void) | null = null
  /** @type {number | null} */
  let pointer: number | null = null
  let lastX = 0
  let lastY = 0
  /** @type {ReturnType<typeof setTimeout> | null} */
  let finishTimer: ReturnType<typeof setTimeout> | null = null
  const controller = new AbortController()
  const opts = { capture: true, passive: false, signal: controller.signal }
  /** @param {Event} event */
  const inside = (event: Event) => event.composedPath().includes(surface)
  /** @param {Event} event */
  const block = (event: Event) => {
    event.preventDefault()
    event.stopImmediatePropagation()
  }
  function release() {
    if (finishTimer !== null) {
      clearTimeout(finishTimer)
      finishTimer = null
    }
    const id = pointer
    pointer = null
    if (id !== null && surface.hasPointerCapture(id)) {
      surface.releasePointerCapture(id)
    }
  }
  const unsubscribe = interaction.onCancel(() => {
    mask?.cancel()
    release()
  })
  window.addEventListener(
    'pointerdown',
    (event) => {
      if (!inside(event)) {
        return
      }
      if (pointer !== null) {
        block(event)
        return
      }
      // 先让蒙版会话判断是否点击了其他对象；退出后本次按下直接归选择工具。
      if ((event.button === 0 || event.button === 2) && interaction.getState().mode === 'mask') {
        mask?.beforeDown?.(event)
      }
      if (
        interaction.getState().mode === 'recognizing' &&
        interaction.getState().effective !== 'pan' &&
        event.button !== 1
      ) {
        recognitionDown?.(event)
      }
      const owner = interaction.begin(event.button)
      if (!owner) {
        if (!interaction.canEdit()) {
          block(event)
        }
        return
      }
      pointer = event.pointerId
      lastX = event.clientX
      lastY = event.clientY
      if (owner !== 'select') {
        block(event)
        surface.setPointerCapture(pointer)
        if (owner === 'mask') {
          mask?.down(event)
        }
      }
    },
    opts,
  )
  window.addEventListener(
    'pointermove',
    (event) => {
      if (event.pointerId !== pointer) {
        return
      }
      const owner = interaction.getState().gesture
      if (owner === 'select') {
        return
      }
      block(event)
      if (owner === 'pan') {
        camera.pan(event.clientX - lastX, event.clientY - lastY)
      }
      if (owner === 'mask') {
        mask?.move(event)
      }
      lastX = event.clientX
      lastY = event.clientY
    },
    opts,
  )
  window.addEventListener(
    'pointerup',
    (event) => {
      if (event.pointerId !== pointer) {
        return
      }
      const owner = interaction.getState().gesture
      if (owner === 'select') {
        // 让同一轮兼容 mouseup / touchend 和 Konva dragend 先完成再切换工具。
        finishTimer = setTimeout(() => {
          release()
          interaction.end()
        }, 0)
      } else {
        block(event)
        if (owner === 'mask') {
          mask?.up()
        }
        release()
        interaction.end()
      }
    },
    opts,
  )
  window.addEventListener(
    'pointercancel',
    (event) => {
      if (event.pointerId === pointer) {
        interaction.cancel()
      }
    },
    opts,
  )
  surface.addEventListener(
    'lostpointercapture',
    (event) => {
      if (event.pointerId === pointer && finishTimer === null) {
        interaction.cancel()
      }
    },
    { signal: controller.signal },
  )
  window.addEventListener('blur', () => interaction.cancel(), { signal: controller.signal })
  document.addEventListener(
    'visibilitychange',
    () => {
      if (document.hidden) {
        interaction.cancel()
      }
    },
    { signal: controller.signal },
  )
  // 阻止浏览器兼容鼠标/触摸事件穿透到 Konva 原生拖动和其他旧监听。
  for (const type of [
    'mousedown',
    'mousemove',
    'mouseup',
    'touchstart',
    'touchmove',
    'touchend',
    'click',
    'dblclick',
    'contextmenu',
    'dragover',
    'drop',
  ]) {
    window.addEventListener(
      type,
      (event) => {
        if (inside(event) && !interaction.canEdit()) {
          block(event)
        }
      },
      opts,
    )
  }
  surface.addEventListener(
    'wheel',
    (event) => {
      block(event)
      if (interaction.canNavigate() && !interaction.getState().gesture) {
        camera.wheel(event)
      }
    },
    opts,
  )
  window.addEventListener(
    'keydown',
    (event) => {
      if (event.isComposing || isEditingInput(event)) {
        return
      }
      const state = interaction.getState()
      if (event.code === 'Space' && !event.ctrlKey && !event.metaKey && !event.altKey) {
        // 原生按钮和弹窗保持正常键盘激活。
        if (
          event
            .composedPath()
            .some(
              (node) =>
                node instanceof HTMLElement &&
                node.matches('button, summary, dialog, [role="dialog"]'),
            )
        ) {
          return
        }
        block(event)
        if (!event.repeat) {
          interaction.setSpace(true)
        }
        return
      }
      if (state.mode === 'recognizing') {
        event.stopImmediatePropagation()
        if (event.key === 'Escape') {
          event.preventDefault()
          cancelRecognition()
        } else if (!['Tab', 'Enter', ' '].includes(event.key)) {
          event.preventDefault()
        }
        return
      }
      if (event.key === 'Escape' && state.gesture) {
        block(event)
        interaction.cancel()
        return
      }
      const shortcut =
        ['Escape', 'Delete', 'Backspace'].includes(event.key) ||
        ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z')
      if (state.mode === 'mask' && shortcut) {
        block(event)
        if (!state.gesture) {
          mask?.key(event)
        }
        return
      }
      if (shortcut && (state.effective !== 'select' || state.gesture)) {
        block(event)
        if (event.key === 'Escape' && state.mode === 'pan') {
          interaction.setMode('select')
        }
        return
      }
      if (state.mode === 'mask') {
        return
      }
      selectionKey(event)
    },
    opts,
  )
  window.addEventListener(
    'keyup',
    (event) => {
      if (event.code === 'Space') {
        interaction.setSpace(false)
      }
    },
    opts,
  )
  return {
    /** @param {MaskHandlers | null} handlers */
    setMaskHandlers(handlers: MaskHandlers | null) {
      mask = handlers
    },
    /** @param {typeof selectionKey} handler */
    setSelectionKey(handler: typeof selectionKey) {
      selectionKey = handler
    },
    /** @param {() => void} handler */
    setRecognitionCancel(handler: () => void) {
      cancelRecognition = handler
    },
    setRecognitionPointer(handler: ((event: PointerEvent) => void) | null) {
      recognitionDown = handler
    },
    destroy() {
      interaction.cancel()
      unsubscribe()
      controller.abort()
    },
  }
}
