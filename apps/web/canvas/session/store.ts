/** Database sequences are authoritative across windows; local storage only retains retries. */
export type SessionMessage = {
  id: string
  sequence: number
  role: string
  content: { text?: string; [key: string]: unknown }
  run_id: string | null
  processing_status: string
}
export type SessionRun = {
  id: string
  status: string
  version: number
  credits_limit?: number
  credits_spent?: number
  [key: string]: unknown
}
export type SessionSnapshot = {
  id: string
  cursor: number
  summary: string
  messages: SessionMessage[]
  runs: SessionRun[]
  notifications?: { id: string; kind: string; body: { status?: string; run_id?: string } }[]
}
export type SessionArtifact = {
  id: string
  ordinal: number
  core_asset_id: string
  asset_id: string
  canvas_node_id: string | null
}
export type VersionSwitch = {
  document_id: string
  node_id: string
  expected_document_revision: number
  expected_anchor_version: number
  idempotency_key: string
}
export type PendingMessage = {
  client_message_id: string
  client_id: string
  message: string
  selected_assets: string[]
  selected_node_ids?: string[]
  credits_budget?: number
}
export type SessionApi = {
  open(documentId: string): Promise<SessionSnapshot>
  snapshot(id: string): Promise<SessionSnapshot>
  send(id: string, body: PendingMessage): Promise<unknown>
  events(id: string, cursor: number): string
  heartbeat?(id: string, clientId: string, cursor: number, online: boolean): Promise<unknown>
  artifacts?(id: string): Promise<{ artifacts: SessionArtifact[] }>
  versions?(id: string): Promise<import('./version-history.js').AssetVersion[]>
  switchVersion?(assetId: string, versionId: string, body: VersionSwitch): Promise<void>
  readNotification?(id: string): Promise<unknown>
}
export type SessionSource = {
  addEventListener(
    name: string,
    listener: (event: { data?: string; lastEventId?: string }) => void,
  ): void
  close(): void
}
export type SessionState = {
  snapshot: SessionSnapshot | null
  pending: PendingMessage[]
  connected: boolean
  error: string
}
export type SessionStoreOptions = {
  documentId: string
  clientId: string
  api: SessionApi
  storage: Storage
  source: (url: string) => SessionSource
  newId?: () => string
}

function readSnapshot(value: unknown): SessionSnapshot {
  if (!value || typeof value !== 'object') {
    throw new Error('会话快照无法读取')
  }
  const row = value as Record<string, unknown>
  if (
    typeof row.id !== 'string' ||
    !Number.isSafeInteger(row.cursor) ||
    Number(row.cursor) < 0 ||
    typeof row.summary !== 'string' ||
    !Array.isArray(row.messages) ||
    !Array.isArray(row.runs)
  ) {
    throw new Error('会话快照无法读取')
  }
  for (const message of row.messages) {
    if (!message || typeof message !== 'object') {
      throw new Error('消息记录无法读取')
    }
    const item = message as Record<string, unknown>
    if (
      typeof item.id !== 'string' ||
      !Number.isSafeInteger(item.sequence) ||
      typeof item.role !== 'string' ||
      !item.content ||
      typeof item.content !== 'object' ||
      typeof item.processing_status !== 'string' ||
      (item.run_id !== null && typeof item.run_id !== 'string')
    ) {
      throw new Error('消息记录无法读取')
    }
  }
  for (const run of row.runs) {
    if (!run || typeof run !== 'object') {
      throw new Error('运行记录无法读取')
    }
    const item = run as Record<string, unknown>
    if (
      typeof item.id !== 'string' ||
      typeof item.status !== 'string' ||
      !Number.isSafeInteger(item.version)
    ) {
      throw new Error('运行记录无法读取')
    }
  }
  return row as SessionSnapshot
}

export function createSessionStore(options: SessionStoreOptions) {
  const listeners = new Set<() => void>()
  let storagePrefix = ''
  let localSequence = 0
  const storageKey = (row: PendingMessage) =>
    `${storagePrefix}${encodeURIComponent(row.client_id)}:${encodeURIComponent(row.client_message_id)}`
  const restore = (): PendingMessage[] => {
    const rows: { sequence: number; message: PendingMessage }[] = []
    for (let index = 0; index < options.storage.length; index++) {
      const key = options.storage.key(index)
      if (!key?.startsWith(storagePrefix)) {
        continue
      }
      try {
        const saved: unknown = JSON.parse(options.storage.getItem(key) ?? 'null')
        if (!saved || typeof saved !== 'object') {
          continue
        }
        const envelope = saved as Record<string, unknown>
        if (
          !Number.isSafeInteger(envelope.sequence) ||
          !envelope.message ||
          typeof envelope.message !== 'object'
        ) {
          continue
        }
        const row = envelope.message as Record<string, unknown>
        if (
          typeof row.client_message_id !== 'string' ||
          typeof row.client_id !== 'string' ||
          typeof row.message !== 'string' ||
          !Array.isArray(row.selected_assets) ||
          !row.selected_assets.every((id: unknown) => typeof id === 'string') ||
          (row.selected_node_ids !== undefined &&
            (!Array.isArray(row.selected_node_ids) ||
              !row.selected_node_ids.every((id: unknown) => typeof id === 'string'))) ||
          (row.credits_budget !== undefined &&
            (!Number.isSafeInteger(row.credits_budget) || Number(row.credits_budget) < 0))
        ) {
          continue
        }
        const message = row as PendingMessage
        if (key !== storageKey(message)) {
          continue
        }
        const sequence = Number(envelope.sequence)
        if (message.client_id === options.clientId) {
          localSequence = Math.max(localSequence, sequence)
        }
        rows.push({ sequence, message })
      } catch {
        /* Corrupt local records never become authoritative messages. */
      }
    }
    rows.sort(
      (left, right) =>
        left.message.client_id.localeCompare(right.message.client_id) ||
        left.sequence - right.sequence,
    )
    return rows.map((row) => row.message)
  }
  let state: SessionState = { snapshot: null, pending: [], connected: false, error: '' }
  let source: SessionSource | undefined
  let generation = 0
  let active = false
  let refreshing: Promise<void> | undefined
  let refreshAgain = false
  let flushing: Promise<void> | undefined
  const publish = (patch: Partial<SessionState>) => {
    state = { ...state, ...patch }
    for (const listener of listeners) {
      listener()
    }
  }
  const save = (item: PendingMessage) => {
    localSequence++
    options.storage.setItem(
      storageKey(item),
      JSON.stringify({ sequence: localSequence, message: item }),
    )
    publish({ pending: [...state.pending, item] })
  }
  const acknowledged = (item: PendingMessage) => {
    options.storage.removeItem(storageKey(item))
    publish({
      pending: state.pending.filter(
        (row) =>
          row.client_id !== item.client_id || row.client_message_id !== item.client_message_id,
      ),
    })
  }
  const apply = (input: SessionSnapshot, reset = false) => {
    const snapshot = readSnapshot(input)
    const old = state.snapshot
    if (!reset && old?.id === snapshot.id && old.cursor > snapshot.cursor) {
      return
    }
    publish({
      snapshot: {
        ...snapshot,
        messages: [...snapshot.messages].sort((a, b) => a.sequence - b.sequence),
      },
      error: '',
    })
  }
  const refresh = (): Promise<void> => {
    if (!active) {
      return Promise.resolve()
    }
    if (refreshing) {
      refreshAgain = true
      return refreshing
    }
    const epoch = generation
    const id = state.snapshot?.id
    if (!id) {
      return Promise.resolve()
    }
    const work = (async () => {
      do {
        refreshAgain = false
        const snapshot = await options.api.snapshot(id)
        if (active && epoch === generation) {
          apply(snapshot)
        }
      } while (refreshAgain && active && epoch === generation)
    })()
      .catch((error: unknown) => {
        if (active && epoch === generation) {
          publish({ error: String(error) })
        }
      })
      .finally(() => {
        if (refreshing === work) {
          refreshing = undefined
        }
      })
    refreshing = work
    return work
  }
  const connect = () => {
    source?.close()
    const snapshot = state.snapshot
    if (!snapshot || !active) {
      return
    }
    const epoch = generation
    source = options.source(options.api.events(snapshot.id, snapshot.cursor))
    source.addEventListener('open', () => {
      if (epoch === generation && active) {
        publish({ connected: true })
      }
    })
    source.addEventListener('error', () => {
      if (epoch === generation && active) {
        publish({ connected: false })
      }
      // EventSource reconnects using Last-Event-ID; a fresh snapshot repairs missed wakeups.
      if (epoch === generation && active) {
        void refresh()
      }
    })
    source.addEventListener('snapshot', (event) => {
      if (epoch !== generation || !active) {
        return
      }
      try {
        apply(readSnapshot(JSON.parse(event.data ?? '') as unknown), true)
      } catch {
        publish({ error: '会话快照无法读取' })
      }
    })
    const changed = (event: { lastEventId?: string }) => {
      if (epoch !== generation || !active) {
        return
      }
      const sequence = Number(event.lastEventId)
      if (!Number.isSafeInteger(sequence) || sequence <= (state.snapshot?.cursor ?? 0)) {
        return
      }
      // The journal contains IDs only. Read database projection instead of trusting event data.
      void refresh()
    }
    source.addEventListener('session', changed)
    source.addEventListener('message', changed)
  }
  const flush = (): Promise<void> => {
    if (flushing) {
      return flushing
    }
    const epoch = generation
    const work = (async () => {
      while (active && epoch === generation && state.snapshot && state.pending[0]) {
        const item = state.pending[0]
        try {
          await options.api.send(state.snapshot.id, item)
          if (epoch !== generation || !active) {
            return
          }
          acknowledged(item)
        } catch (error) {
          if (epoch === generation && active) {
            publish({ error: String(error) })
          }
          return
        }
      }
      await refresh()
    })().finally(() => {
      if (flushing === work) {
        flushing = undefined
      }
    })
    flushing = work
    return work
  }
  return {
    getSnapshot: () => state,
    subscribe(listener: () => void) {
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
      }
    },
    start() {
      active = true
      const epoch = ++generation
      source?.close()
      refreshing = flushing = undefined
      publish({ snapshot: null, pending: [], connected: false, error: '' })
      void options.api
        .open(options.documentId)
        .then(async (snapshot) => {
          if (!active || epoch !== generation) {
            return
          }
          apply(snapshot, true)
          storagePrefix = `gencanvas.core.pending.${snapshot.id}.`
          publish({ pending: restore() })
          connect()
          await flush()
        })
        .catch((error: unknown) => {
          if (active && epoch === generation) {
            publish({ error: String(error) })
          }
        })
      return () => {
        active = false
        generation++
        source?.close()
        publish({ connected: false })
      }
    },
    send(
      message: string,
      selectedAssets: readonly string[] = [],
      creditsBudget = 0,
      selectedNodes: readonly string[] = [],
    ) {
      if (!message.trim()) {
        return Promise.resolve()
      }
      if (!Number.isSafeInteger(creditsBudget) || creditsBudget < 0) {
        return Promise.reject(new Error('积分预算无效'))
      }
      if (!state.snapshot) {
        throw new Error('会话尚未连接，请稍后发送')
      }
      save({
        client_message_id: (options.newId ?? (() => crypto.randomUUID()))(),
        client_id: options.clientId,
        message: message.trim(),
        selected_assets: [...selectedAssets],
        selected_node_ids: [...selectedNodes],
        credits_budget: creditsBudget,
      })
      return flush()
    },
    retry: flush,
    refresh,
    async markRead(id: string) {
      try {
        await options.api.readNotification?.(id)
        await refresh()
      } catch (error) {
        publish({ error: String(error) })
      }
    },
  }
}
