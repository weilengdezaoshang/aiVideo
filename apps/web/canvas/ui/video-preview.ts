/** 浏览器原生播放器：关闭后立即停止播放并释放媒体连接。 */
export function openVideoPreview(url: string, title: string): void {
  const dialog = document.createElement('dialog')
  dialog.setAttribute('aria-label', title)
  dialog.style.cssText =
    'padding:16px;border:1px solid var(--studio-hairline);border-radius:16px;background:var(--studio-panel);color:var(--studio-ink);max-width:90vw;'
  const close = document.createElement('button')
  close.textContent = '关闭视频'
  close.style.cssText = 'display:block;margin:0 0 12px auto;padding:8px 12px;cursor:pointer;'
  const video = document.createElement('video')
  video.controls = true
  video.playsInline = true
  video.src = url
  video.style.cssText = 'display:block;max-width:85vw;max-height:75vh;background:#000;'
  close.addEventListener('click', () => dialog.close())
  dialog.addEventListener(
    'close',
    () => {
      video.pause()
      video.removeAttribute('src')
      video.load()
      dialog.remove()
    },
    { once: true },
  )
  dialog.append(close, video)
  document.body.append(dialog)
  dialog.showModal()
  void video.play().catch(() => {
    /* 原生播放按钮可在自动播放受限时继续播放。 */
  })
}
