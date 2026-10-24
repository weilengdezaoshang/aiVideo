import {
  assertRequestNodesUnchanged,
  buildRequestContext,
  captureRequestContext,
  inferBatchIntent,
  type CanvasRequestSnapshot,
} from './request-context.js'
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
  hostEnabled: boolean
  pendingCanvasResults: Record<string, string>
}
export type AgentStoreOptions = {
  documentId: string
  onCanvasAction?: (run: AgentRun, automatic: boolean) => void
  captureContext?: (selection: readonly string[]) => CanvasRequestSnapshot
  ensureSaved: () => Promise<number>
  api?: AgentApi
  syncDocument?: () => Promise<void>
  documentRevision?: () => number
  storage?: Storage
  eventSourceFactory?: (url: string) => AgentEventSource
}

/** 澄清短答绑定原快照；新的完整创作指令应建立新决策。 */
function clarificationFor(message: string, runs: readonly AgentRun[]): string | undefined {
  const pending = runs.filter((run) => run.status === 'waiting_input').at(-1)
  if (!pending) {
    return undefined
  }
  const answer = message.trim()
  const labels =
    pending.waiting && 'question' in pending.waiting
      ? (pending.waiting.options?.map((option) => option.label) ?? [])
      : []
  const replies = [
    '改图',
    '图片',
    '生成图片',
    '做成视频',
    '视频',
    '一次做完',
    '先做第一步',
    '先做A',
  ]
  if (replies.includes(answer) || labels.includes(answer)) {
    return pending.id
  }
  // 这里只区分会话回复与新指令，不在前端判断 L2。
  if (
    /^(?:请\s*)?(?:生成|画|绘制|创建|制作|把|先|帮我|重新|另外|另做|再生成|generate\b|create\b|draw\b|make\b|edit\b)/i.test(
      answer,
    )
  ) {
    return undefined
  }
  return pending.id
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
  const canvasResultsKey = `gencanvas.agent.canvas-results.${options.documentId}`
  const readCanvasResults = (): Record<string, string> => {
    try {
      const value: unknown = JSON.parse(storage.getItem(canvasResultsKey) ?? '{}')
      if (value && typeof value === 'object' && !Array.isArray(value)) {
        return Object.fromEntries(
          Object.entries(value).filter(
            (item): item is [string, string] => typeof item[1] === 'string',
          ),
        )
      }
    } catch {
      /* Invalid receipts never become canvas actions. */
    }
    return {}
  }
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
    hostEnabled: false,
    pendingCanvasResults: readCanvasResults(),
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
    const pendingCanvasResults = { ...state.pendingCanvasResults }
    if (
      run.canvasAction &&
      ['completed', 'cancelled'].includes(run.status) &&
      pendingCanvasResults[run.id]
    ) {
      delete pendingCanvasResults[run.id]
      storage.setItem(canvasResultsKey, JSON.stringify(pendingCanvasResults))
    }
    publish({ runs, pendingCanvasResults })
    if (
      run.media?.documentRevision &&
      options.syncDocument &&
      run.media.documentRevision > (options.documentRevision?.() ?? 0)
    ) {
      void options.syncDocument().catch((error: unknown) => publish({ error: messageOf(error) }))
    }
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
    if (snapshot.sessionId) {
      publish({ sessionId: snapshot.sessionId })
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
        `${api.eventPath?.(options.documentId) ?? `/api/documents/${encodeURIComponent(options.documentId)}/agent/events`}?after=${state.cursor}`,
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
    const token = observation
    try {
      const { run, replayed } = await api.create(options.documentId, request)
      if (!run?.id || run.clientRequestId !== request.clientRequestId) {
        throw new Error('助手受理结果待确认')
      }
      mergeRun(run)
      acknowledge(run)
      publish({ error: '' })
      if (request.context && !state.sessionId) {
        await refresh()
      }
      // Only a fresh acknowledgement opens the canvas. SSE/history/retries never rerun recognition.
      const current = state.runs.find((item) => item.id === run.id)
      if (
        !replayed &&
        observing &&
        token === observation &&
        current?.canvasAction &&
        current.status === 'waiting_approval'
      ) {
        try {
          options.onCanvasAction?.(current, true)
        } catch (error: unknown) {
          publish({ error: messageOf(error) })
        }
      }
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
    openCanvasAction(runId: string): void {
      const run = state.runs.find((item) => item.id === runId)
      if (!run?.canvasAction || run.status !== 'waiting_approval') {
        return
      }
      if (state.pendingCanvasResults[runId]) {
        publish({ error: '抠图结果已生成，请先同步结果，无需重新识别' })
        return
      }
      try {
        if (!options.onCanvasAction) {
          throw new Error('请在画布页面打开此抠图任务')
        }
        options.onCanvasAction(run, false)
        publish({ error: '' })
      } catch (error: unknown) {
        publish({ error: messageOf(error) })
      }
    },
    async completeCanvasAction(runId: string, resultObjectId: string): Promise<void> {
      if (state.runs.find((run) => run.id === runId)?.status === 'cancelled') {
        return
      }
      try {
        const pendingCanvasResults = { ...state.pendingCanvasResults, [runId]: resultObjectId }
        publish({ pendingCanvasResults })
        storage.setItem(canvasResultsKey, JSON.stringify(pendingCanvasResults))
        if (!api.completeCanvasAction) {
          throw new Error('当前助手未支持抠图结果回写')
        }
        const revision = await options.ensureSaved()
        const { run } = await api.completeCanvasAction(runId, resultObjectId, revision)
        mergeRun(run)
        publish({ error: '' })
      } catch (error: unknown) {
        publish({ error: `抠图结果已保留，助手状态同步失败：${messageOf(error)}` })
        throw error
      }
    },
    async retryCanvasResult(runId: string): Promise<void> {
      const resultObjectId = state.pendingCanvasResults[runId]
      if (!resultObjectId) {
        return
      }
      try {
        const revision = await options.ensureSaved()
        if (!api.completeCanvasAction) {
          throw new Error('当前助手未支持抠图结果回写')
        }
        const { run } = await api.completeCanvasAction(runId, resultObjectId, revision)
        mergeRun(run)
        publish({ error: '' })
      } catch (error: unknown) {
        publish({ error: messageOf(error) })
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
          publish({
            models: result.models,
            modelId: state.modelId || result.defaultModelId || '',
            hostEnabled: result.hostEnabled === true,
          })
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
    async send(
      selectedObjectIds: readonly string[],
      clarify?: { runId: string; optionId?: string } | null,
    ): Promise<boolean> {
      if (
        state.sending ||
        !state.draft.trim() ||
        !state.modelId ||
        state.loading ||
        (!state.hostEnabled && state.runs.some(isActiveAgentRun)) ||
        readPending().length
      ) {
        return false
      }
      const message = state.draft.trim()
      const modelId = state.modelId
      const selection = [...selectedObjectIds]
      const clarifyRunId =
        clarify === null
          ? undefined
          : (clarify?.runId ??
            (state.hostEnabled ? clarificationFor(message, state.runs) : undefined))
      publish({ sending: true, error: '' })
      try {
        const contextSnapshot =
          state.hostEnabled && options.captureContext
            ? captureRequestContext(options.captureContext(selection))
            : undefined
        if (contextSnapshot && state.runs.length && !state.sessionId) {
          await refresh()
          if (!state.sessionId) {
            throw new Error('无法确认当前画布会话，请重新连接后重试')
          }
        }
        const baseRevision = await options.ensureSaved()
        if (contextSnapshot && options.captureContext) {
          assertRequestNodesUnchanged(contextSnapshot, options.captureContext(selection))
        }
        const requestId = crypto.randomUUID()
        const request: AgentRequest = {
          ...(contextSnapshot
            ? {
                context: buildRequestContext({
                  requestId,
                  conversationId: state.sessionId ?? crypto.randomUUID(),
                  documentId: options.documentId,
                  revision: baseRevision,
                  message,
                  snapshot: contextSnapshot,
                }),
              }
            : {}),
          clientRequestId: requestId,
          message,
          modelId,
          baseRevision,
          selectedObjectIds: selection,
          ...(inferBatchIntent(message, selection.length) ? { batch: true } : {}),
          ...(clarifyRunId ? { clarifyRunId } : {}),
          ...(clarify?.optionId ? { clarifyOptionId: clarify.optionId } : {}),
          ...(state.hostEnabled &&
          !/^(同时|另外|再生成|另做)/.test(message) &&
          state.runs.some(isActiveAgentRun)
            ? {
                supersedeRunId:
                  state.runs
                    .filter((run) => isActiveAgentRun(run) && run.status !== 'canceling')
                    .at(-1)?.id ?? state.runs.filter(isActiveAgentRun).at(-1)!.id,
              }
            : {}),
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
    async retryBatchItems(runId: string, itemIds: string[]): Promise<void> {
      if (state.sending || !api.retryBatchItems || itemIds.length === 0) {
        return
      }
      publish({ sending: true, error: '' })
      const key = `${prefix}batch-retry.${runId}`
      try {
        const requestKey = storage.getItem(key) || crypto.randomUUID()
        storage.setItem(key, requestKey)
        await api.retryBatchItems(runId, requestKey, itemIds)
        await refresh()
      } catch (error) {
        publish({ error: messageOf(error) })
      } finally {
        publish({ sending: false })
      }
    },
    async retry(runId: string, stepId: string, index: number): Promise<void> {
      if (state.sending || !api.retry) {
        return
      }
      publish({ sending: true, error: '' })
      const key = `${prefix}retry.${runId}.${stepId}.${index}`
      try {
        const requestId = storage.getItem(key) || crypto.randomUUID()
        storage.setItem(key, requestId)
        const { run } = await api.retry(runId, requestId, stepId, index)
        mergeRun(run)
        storage.removeItem(key)
      } catch (error) {
        publish({ error: messageOf(error) })
        await refresh()
      } finally {
        publish({ sending: false })
      }
    },
    async confirm(runId: string, approved: boolean): Promise<void> {
      if (state.sending || !api.confirm) {
        return
      }
      publish({ sending: true, error: '' })
      const key = `${prefix}confirm.${runId}`
      try {
        const stored = storage.getItem(key)
        const request = stored
          ? (JSON.parse(stored) as { requestId: string; approved: boolean })
          : { requestId: crypto.randomUUID(), approved }
        if (request.approved !== approved) {
          throw new Error('上次确认结果尚未确定，请查询原决定后再操作')
        }
        storage.setItem(key, JSON.stringify(request))
        const { run } = await api.confirm(runId, request.requestId, request.approved)
        mergeRun(run)
        storage.removeItem(key)
      } catch (error) {
        publish({ error: messageOf(error) })
      } finally {
        publish({ sending: false })
      }
    },
    async final(runId: string): Promise<void> {
      if (state.sending || !api.final) {
        return
      }
      publish({ sending: true, error: '' })
      const key = `${prefix}final.${runId}`
      try {
        const baseRevision = await options.ensureSaved()
        const stored = storage.getItem(key)
        const request = stored
          ? (JSON.parse(stored) as { requestId: string; baseRevision: number })
          : { requestId: crypto.randomUUID(), baseRevision }
        storage.setItem(key, JSON.stringify(request))
        const { run } = await api.final(runId, request.requestId, request.baseRevision)
        mergeRun(run)
        storage.removeItem(key)
        await refresh()
      } catch (error) {
        publish({ error: messageOf(error) })
        await refresh()
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
