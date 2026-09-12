// 单标签页约束检测(PRD §9):BroadcastChannel 握手,发现第二个画布页即提示。
// 不强制关闭(用户可能有别的用途),只警告文档会被互相覆盖。

const CHANNEL = 'gencanvas.pages'

export function watchDuplicateTabs(options: { onDuplicate?: () => void } = {}): {
  close(): void
} {
  const { onDuplicate = () => {} } = options
  if (typeof BroadcastChannel === 'undefined') {
    return { close() {} }
  }
  let channel: BroadcastChannel
  try {
    channel = new BroadcastChannel(CHANNEL)
  } catch {
    return { close() {} }
  }
  let warned = false
  const announceTimer = setTimeout(() => {
    // 静默期结束;此后只在新的 hello 到达时再警告
  }, 500)

  channel.onmessage = (event: MessageEvent) => {
    if (event.data === 'hello') {
      // 对方刚打开:回应自己存在,并警告
      channel.postMessage('exists')
      if (!warned) {
        warned = true
        onDuplicate()
      }
    } else if (event.data === 'exists') {
      if (!warned) {
        warned = true
        onDuplicate()
      }
    }
  }

  // 宣告自己上线;稍等片刻收集回应
  channel.postMessage('hello')

  return {
    close() {
      clearTimeout(announceTimer)
      channel.close()
    },
  }
}
