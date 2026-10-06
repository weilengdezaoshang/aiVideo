import { AgentApiError, createAgentApi, type AgentApi } from './api.js'
import {
  isActiveAgentRun,
  type AgentEvent,
  type AgentModel,
  type AgentRequest,
  type AgentRun,
  type AgentSnapshot,
} from './types.js'

export type AgentEventSource = {
  addEventListener(type: string, listener: (event: { data?: string }) => void): void
  close(): void
}
export type AgentStoreSnapshot = {
  sessionId: string | null
  cursor: number
  models: AgentModel[]
  modelId: string
  runs: AgentRun[]
  pendingRequests: AgentRequest[]
  draft: string
  loading: boolean
  connected: boolean
  sending: boolean
  error: string
}
export type AgentStoreOptions = {
  documentId: string
  ensureSaved: () => Promise<number>
  api?: AgentApi
  storage?: Storage
  eventSourceFactory?: (url: string) => AgentEventSource
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : '助手连接失败'
}
function validPending(value: unknown): value is AgentRequest {
  if (!value || typeof value !== 'object') {
    return false
  }
  const item = value as Record<string, unknown>
  return (
    typeof item.clientRequestId === 'string' &&
    typeof item.message === 'string' &&
    typeof item.modelId === 'string' &&
    Number.isInteger(item.baseRevision) &&
    Array.isArray(item.selectedObjectIds) &&
    item.selectedObjectIds.every((id) => typeof id === 'string')
  )
}

/** Agent state is independent of the canvas document, autosave and undo history. */
export function createAgentStore(options: AgentStoreOptions) {
  const api = options.api ?? createAgentApi()
  const storage = options.storage ?? localStorage
  const prefix = `gencanvas.agent.pending.${options.documentId}.`
  const listeners = new Set<() => void>()
  const readPending = (): AgentRequest[] => {
    const pending: AgentRequest[] = []
    for (let index = 0; index < storage.length; index++) {
      const key = storage.key(index)
      if (!key?.startsWith(prefix)) {
        continue
      }
      try {
        const value: unknown = JSON.parse(storage.getItem(key) ?? 'null')
        if (validPending(value) && key === prefix + value.clientRequestId) {
          pending.push(value)
        }
      } catch {
        /* An unrelated or corrupt entry must never become a submission. */
      }
    }
    return pending
  }
  let state: AgentStoreSnapshot = {
    sessionId: null,
    cursor: 0,
    models: [],
    modelId: '',
    runs: [],
    pendingRequests: readPending(),
    draft: '',
    loading: true,
    connected: false,
    sending: false,
    error: '',
  }
  if (state.pendingRequests[0]) {
    state.draft = state.pendingRequests[0].message
  }
  let observation = 0
  let observing = false
  let source: AgentEventSource | undefined
  let retryTimer: ReturnType<typeof setTimeout> | undefined
  let refreshing: Promise<void> | undefined
  let refreshObservation = -1
  const publish = (patch: Partial<AgentStoreSnapshot>) => {
    state = { ...state, ...patch }
    for (const listener of listeners) {
      listener()
    }
  }
  const mergeRun = (run: AgentRun) => {
    const existing = state.runs.find((item) => item.id === run.id)
    if (
      existing?.stateVersion !== undefined &&
      run.stateVersion !== undefined &&
      existing.stateVersion > run.stateVersion
    ) {
      return
    }
    if (
      existing &&
      (existing.stateVersion === undefined || run.stateVersion === undefined) &&
      Date.parse(existing.updatedAt) > Date.parse(run.updatedAt)
    ) {
      return
    }
    const runs = state.runs.filter((item) => item.id !== run.id)
    runs.push(run)
    runs.sort((a, b) => a.createdAt.localeCompare(b.createdAt) || a.id.localeCompare(b.id))
    publish({ runs })
  }
  const acknowledge = (run: AgentRun) => {
    const pending = state.pendingRequests.find(
      (item) => item.clientRequestId === run.clientRequestId,
    )
    storage.removeItem(prefix + run.clientRequestId)
    publish({
      pendingRequests: readPending(),
      ...(pending && state.draft.trim() === pending.message ? { draft: '' } : {}),
    })
  }
  const applySnapshot = (snapshot: AgentSnapshot) => {
    if (snapshot.documentId !== options.documentId) {
      throw new Error('助手返回了其他画布的会话')
    }
    if (snapshot.cursor >= state.cursor) {
      publish({ sessionId: snapshot.sessionId, cursor: snapshot.cursor })
      for (const run of snapshot.runs) {
        mergeRun(run)
        acknowledge(run)
      }
    }
    if (snapshot.requestedRun) {
      mergeRun(snapshot.requestedRun)
      acknowledge(snapshot.requestedRun)
    }
  }
  async function refresh(): Promise<void> {
    if (refreshing && refreshObservation === observation) {
      return refreshing
    }
    const token = observation
    refreshObservation = token
    const task = (async () => {
      try {
        publish({ pendingRequests: readPending() })
        const pending = [...state.pendingRequests]
        const first = await api.snapshot(options.documentId, pending[0]?.clientRequestId)
        if (token !== observation) {
          return
        }
        applySnapshot(first)
        for (const request of pending.slice(1)) {
          const snapshot = await api.snapshot(options.documentId, request.clientRequestId)
          if (token !== observation) {
            return
          }
          applySnapshot(snapshot)
        }
        publish({ loading: false, error: '' })
      } catch (error) {
        if (token === observation) {
          publish({ loading: false, error: messageOf(error) })
        }
      }
    })()
    refreshing = task
    void task.finally(() => {
      if (refreshing === task) {
        refreshing = undefined
      }
    })
    return task
  }
  function scheduleReconnect(token: number) {
    if (!observing || token !== observation || retryTimer !== undefined) {
      return
    }
    retryTimer = setTimeout(() => {
      retryTimer = undefined
      void refresh().then(() => connect(token))
    }, 4000)
  }
  function connect(token: number) {
    if (!observing || token !== observation) {
      return
    }
    source?.close()
    const factory = options.eventSourceFactory ?? ((url: string) => new EventSource(url))
    try {
      source = factory(
        `/api/documents/${encodeURIComponent(options.documentId)}/agent/events?after=${state.cursor}`,
      )
    } catch (error) {
      publish({ connected: false, error: messageOf(error) })
      scheduleReconnect(token)
      return
    }
    source.addEventListener('open', () => {
      if (token === observation) {
        publish({ connected: true })
      }
    })
    source.addEventListener('agent', (event) => {
      if (token !== observation) {
        return
      }
      try {
        const value = JSON.parse(event.data ?? '') as AgentEvent
        if (!Number.isInteger(value.seq) || value.seq <= state.cursor) {
          return
        }
        if (value.seq !== state.cursor + 1 || value.runId !== value.run?.id) {
          source?.close()
          publish({ connected: false })
          void refresh().then(() => connect(token))
          return
        }
        mergeRun(value.run)
        acknowledge(value.run)
        publish({ cursor: value.seq })
      } catch {
        source?.close()
        publish({ connected: false })
        scheduleReconnect(token)
      }
    })
    source.addEventListener('snapshot', (event) => {
      if (token !== observation) {
        return
      }
      try {
        const snapshot = JSON.parse(event.data ?? '') as AgentSnapshot
        if (snapshot.documentId !== options.documentId) {
          throw new Error('会话画布不匹配')
        }
        publish({ cursor: 0 })
        applySnapshot(snapshot)
      } catch {
        source?.close()
        scheduleReconnect(token)
      }
    })
    source.addEventListener('error', () => {
      if (token !== observation) {
        return
      }
      source?.close()
      publish({ connected: false })
      scheduleReconnect(token)
    })
  }
  async function submit(request: AgentRequest): Promise<boolean> {
    try {
      const { run } = await api.create(options.documentId, request)
      if (!run?.id || run.clientRequestId !== request.clientRequestId) {
        throw new Error('助手受理结果待确认')
      }
      mergeRun(run)
      acknowledge(run)
      publish({ error: '' })
      return true
    } catch (error) {
      // Timeouts/5xx do not prove rejection; retry only the exact persisted request.
      if (
        error instanceof AgentApiError &&
        error.status >= 400 &&
        error.status < 500 &&
        error.status !== 408 &&
        error.status !== 429
      ) {
        storage.removeItem(prefix + request.clientRequestId)
        publish({ pendingRequests: readPending(), error: messageOf(error) })
      } else {
        publish({ error: '提交结果待确认。请查询原请求，或使用原请求重试。' })
        await refresh()
      }
      return false
    }
  }
  return {
    getSnapshot: () => state,
    subscribe(listener: () => void) {
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
      }
    },
    setDraft(draft: string) {
      publish({ draft })
    },
    setModel(modelId: string) {
      if (!state.sending) {
        publish({ modelId })
      }
    },
    refresh,
    start() {
      observing = true
      const token = ++observation
      void api
        .models()
        .then((result) => {
          if (token !== observation) {
            return
          }
          publish({ models: result.models, modelId: state.modelId || result.defaultModelId || '' })
        })
        .catch((error: unknown) => {
          if (token === observation) {
            publish({ error: messageOf(error) })
          }
        })
      void refresh().then(() => connect(token))
      const onStorage = (event: StorageEvent) => {
        if (event.key?.startsWith(prefix)) {
          void refresh()
        }
      }
      window.addEventListener('storage', onStorage)
      return () => {
        observing = false
        observation++
        source?.close()
        clearTimeout(retryTimer)
        retryTimer = undefined
        window.removeEventListener('storage', onStorage)
      }
    },
    async send(selectedObjectIds: readonly string[]): Promise<boolean> {
      if (
        state.sending ||
        !state.draft.trim() ||
        !state.modelId ||
        state.loading ||
        state.runs.some(isActiveAgentRun) ||
        readPending().length
      ) {
        return false
      }
      const message = state.draft.trim()
      const modelId = state.modelId
      const selection = [...selectedObjectIds]
      publish({ sending: true, error: '' })
      try {
        const baseRevision = await options.ensureSaved()
        const request: AgentRequest = {
          clientRequestId: crypto.randomUUID(),
          message,
          modelId,
          baseRevision,
          selectedObjectIds: selection,
        }
        // Persistence failure blocks the request: an unrecorded paid call is not recoverable.
        storage.setItem(prefix + request.clientRequestId, JSON.stringify(request))
        publish({ pendingRequests: readPending() })
        return await submit(request)
      } catch (error) {
        publish({ error: messageOf(error) })
        return false
      } finally {
        publish({ sending: false })
      }
    },
    async retryPending(clientRequestId: string): Promise<boolean> {
      if (state.sending) {
        return false
      }
      const request = readPending().find((item) => item.clientRequestId === clientRequestId)
      if (!request) {
        return false
      }
      publish({ sending: true, error: '' })
      try {
        return await submit(request)
      } finally {
        publish({ sending: false })
      }
    },
    async stop(runId: string): Promise<void> {
      try {
        const { run } = await api.stop(runId)
        mergeRun(run)
        publish({ error: '' })
      } catch (error) {
        publish({ error: messageOf(error) })
      }
    },
  }
}

export type AgentStore = ReturnType<typeof createAgentStore>
