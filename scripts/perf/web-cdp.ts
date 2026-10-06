export class WebCDP {
  private socket: WebSocket
  onEvent: (method: string, params: Record<string, unknown>) => void = () => {}
  private sequence = 0
  private pending = new Map<number, { resolve(value: unknown): void; reject(error: Error): void }>()
  constructor(url: string) {
    this.socket = new WebSocket(url)
    this.socket.addEventListener('message', (event) => {
      const message = JSON.parse(String(event.data)) as {
        id?: number
        result?: unknown
        error?: unknown
        method?: string
        params?: Record<string, unknown>
      }
      if (message.id === undefined) {
        if (message.method) {
          this.onEvent(message.method, message.params ?? {})
        }
        return
      }
      const pending = this.pending.get(message.id)
      this.pending.delete(message.id)
      if (message.error) {
        pending?.reject(new Error(JSON.stringify(message.error)))
      } else {
        pending?.resolve(message.result)
      }
    })
    this.socket.addEventListener('close', () => {
      for (const pending of this.pending.values()) {
        pending.reject(new Error('CDP connection closed'))
      }
      this.pending.clear()
    })
  }
  async ready() {
    await new Promise<void>((resolve, reject) => {
      this.socket.addEventListener('open', () => resolve(), { once: true })
      this.socket.addEventListener('error', () => reject(new Error('CDP failed')), { once: true })
    })
  }
  async send<T = Record<string, unknown>>(
    method: string,
    params: Record<string, unknown> = {},
  ): Promise<T> {
    const id = ++this.sequence
    const result = new Promise<unknown>((resolve, reject) =>
      this.pending.set(id, { resolve, reject }),
    )
    const timer = setTimeout(() => {
      this.pending.get(id)?.reject(new Error(`CDP timeout: ${method}`))
      this.pending.delete(id)
    }, 30000)
    this.socket.send(JSON.stringify({ id, method, params }))
    try {
      // CDP JSON is the sole untyped transport boundary; callers supply command result types.
      return (await result) as T
    } finally {
      clearTimeout(timer)
    }
  }
  async evaluate<T>(expression: string): Promise<T> {
    const result = await this.send<{
      result: { value: T }
      exceptionDetails?: unknown
    }>('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })
    if (result.exceptionDetails) {
      throw new Error(JSON.stringify(result.exceptionDetails))
    }
    return result.result.value
  }
  close() {
    this.socket.close()
  }
}
