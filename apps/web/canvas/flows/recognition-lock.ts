/** 图片可见部分的中心；图片不可见时回退视口中心，卡片始终留在视口内。
 * @param {{left:number,top:number,width:number,height:number} | null} anchor
 * @param {{width:number,height:number}} viewport
 * @param {{width:number,height:number}} card */
export function recognitionPosition(
  anchor: { left: number; top: number; width: number; height: number } | null,
  viewport: { width: number; height: number },
  card: { width: number; height: number },
) {
  const left = Math.max(0, anchor?.left ?? 0)
  const top = Math.max(0, anchor?.top ?? 0)
  const right = Math.min(viewport.width, anchor ? anchor.left + anchor.width : viewport.width)
  const bottom = Math.min(viewport.height, anchor ? anchor.top + anchor.height : viewport.height)
  const visible = right > left && bottom > top
  const cx = visible ? (left + right) / 2 : viewport.width / 2
  const cy = visible ? (top + bottom) / 2 : viewport.height / 2
  return {
    left: Math.max(12, Math.min(cx - card.width / 2, viewport.width - card.width - 12)),
    top: Math.max(12, Math.min(cy - card.height / 2, viewport.height - card.height - 12)),
  }
}

/** 识别提示跟随图片；背景可继续选择和平移，不抢焦点。
 * @param {{ onCancel: () => void, getAnchor?: () => {left:number,top:number,width:number,height:number} | null }} options */
export function createRecognitionLock({
  onCancel,
  getAnchor = () => null,
}: {
  onCancel: () => void
  getAnchor?: () => { left: number; top: number; width: number; height: number } | null
}) {
  const dialog = document.createElement('div')
  dialog.className = 'recognition-lock'
  dialog.hidden = true
  let frame: number | null = null
  dialog.setAttribute('aria-label', '正在识别主体')
  const card = document.createElement('div')
  card.className = 'recognition-card'
  const spinner = document.createElement('span')
  spinner.className = 'recognition-spinner'
  spinner.setAttribute('aria-hidden', 'true')
  const message = document.createElement('p')
  message.className = 'recognition-message'
  message.setAttribute('role', 'status')
  message.textContent = '正在识别主体…'
  const cancel = document.createElement('button')
  cancel.type = 'button'
  cancel.className = 'recognition-cancel'
  cancel.textContent = '取消'
  cancel.setAttribute('aria-label', '取消主体识别')
  cancel.addEventListener('click', onCancel)
  card.append(spinner, message, cancel)
  dialog.append(card)
  document.body.append(dialog)

  function position() {
    if (dialog.hidden) {
      return
    }
    const pos = recognitionPosition(
      getAnchor(),
      { width: window.innerWidth, height: window.innerHeight },
      { width: card.offsetWidth, height: card.offsetHeight },
    )
    card.style.left = `${pos.left}px`
    card.style.top = `${pos.top}px`
  }

  function follow() {
    if (dialog.hidden) {
      return
    }
    position()
    if (!dialog.hidden) {
      frame = requestAnimationFrame(follow)
    }
  }

  return {
    lock(label = '正在识别主体…') {
      message.textContent = label
      dialog.setAttribute('aria-label', label)
      if (dialog.hidden) {
        dialog.hidden = false
        window.addEventListener('resize', position)
      }
      if (frame === null) {
        follow()
      }
    },
    unlock() {
      dialog.hidden = true
      if (frame !== null) {
        cancelAnimationFrame(frame)
        frame = null
      }
      window.removeEventListener('resize', position)
    },
  }
}
