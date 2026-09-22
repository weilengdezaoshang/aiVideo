/** A request owns its result only while its source identity and session still match. */
export class RecognitionOperation {
  readonly id = crypto.randomUUID()
  readonly controller = new AbortController()
  private pending = false
  private finished = false

  constructor(private readonly matchesSource: () => boolean) {}

  get signal(): AbortSignal {
    return this.controller.signal
  }

  current(): boolean {
    return !this.finished && !this.signal.aborted && this.matchesSource()
  }

  assertCurrent(): void {
    if (!this.current()) {
      throw new DOMException('识别已取消', 'AbortError')
    }
  }

  async detect(assetId: string): Promise<string> {
    this.assertCurrent()
    this.pending = true
    const response = await fetch(
      `/api/cutout/detect/${encodeURIComponent(assetId)}?operationId=${this.id}`,
      {
        method: 'POST',
        signal: this.signal,
      },
    )
    this.assertCurrent()
    const body: { error?: string; urls?: { original?: string }; operationId?: string } =
      await response.json()
    this.assertCurrent()
    if (!response.ok) {
      throw new Error(body.error || '主体识别失败')
    }
    if (body.operationId !== this.id || !body.urls?.original) {
      throw new Error('主体识别返回数据不完整')
    }
    this.pending = false
    return body.urls.original
  }

  cancel(): void {
    if (this.finished) {
      return
    }
    this.finished = true
    this.controller.abort()
    if (this.pending) {
      void fetch(`/api/cutout/operations/${this.id}`, { method: 'DELETE', keepalive: true }).catch(
        () => {},
      )
    }
  }

  complete(): void {
    this.pending = false
    this.finished = true
  }
}
