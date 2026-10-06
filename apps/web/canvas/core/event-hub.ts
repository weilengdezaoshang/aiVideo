export type EventMap = Record<string, unknown>

export class TypedEventHub<Events extends EventMap> {
  private readonly listeners = new Map<keyof Events, Set<(payload: Events[keyof Events]) => void>>()
  private destroyed = false

  on<K extends keyof Events>(type: K, handler: (payload: Events[K]) => void): () => void {
    if (this.destroyed) {
      return () => {}
    }
    const bucket = this.listeners.get(type) ?? new Set()
    bucket.add(handler as (payload: Events[keyof Events]) => void)
    this.listeners.set(type, bucket)
    return () => bucket.delete(handler as (payload: Events[keyof Events]) => void)
  }

  emit<K extends keyof Events>(type: K, payload: Events[K]): void {
    if (this.destroyed) {
      return
    }
    for (const handler of [...(this.listeners.get(type) ?? [])]) {
      handler(payload)
    }
  }

  destroy(): void {
    this.destroyed = true
    this.listeners.clear()
  }
}
