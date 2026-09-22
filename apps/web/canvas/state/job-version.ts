export type VersionedJob = { id: string; stateVersion?: number; progressSeq?: number }

/** Merge snapshots and SSE without letting a late HTTP response regress state. */
export function latestJob<T extends VersionedJob>(cache: Map<string, T>, incoming: T): T {
  const current = cache.get(incoming.id)
  if (current?.stateVersion !== undefined) {
    if (
      incoming.stateVersion === undefined ||
      incoming.stateVersion < current.stateVersion ||
      (incoming.stateVersion === current.stateVersion &&
        (incoming.progressSeq ?? 0) <= (current.progressSeq ?? 0))
    ) {
      return current
    }
  }
  cache.set(incoming.id, incoming)
  if (cache.size > 4096) {
    const oldest = cache.keys().next().value
    if (oldest) {
      cache.delete(oldest)
    }
  }
  return incoming
}
