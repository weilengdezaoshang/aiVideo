import { withSessionCsrf } from '../../shared/session-fetch.js'
import type { AgentModels, AgentRequest, AgentRun, AgentSnapshot } from './types.js'

export class AgentApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
    this.name = 'AgentApiError'
  }
}

export type AgentApi = {
  models(): Promise<AgentModels>
  snapshot(documentId: string, clientRequestId?: string): Promise<AgentSnapshot>
  create(documentId: string, request: AgentRequest): Promise<{ run: AgentRun; replayed?: boolean }>
  get(runId: string): Promise<{ run: AgentRun }>
  stop(runId: string): Promise<{ run: AgentRun }>
  retry?(
    runId: string,
    requestId: string,
    stepId: string,
    index: number,
  ): Promise<{ run: AgentRun }>
  retryBatchItems?(
    runId: string,
    requestKey: string,
    itemIds: string[],
  ): Promise<{ child_run_id: string | null; replayed: boolean; quote: number }>
  eventPath?(documentId: string): string
  confirm?(runId: string, requestId: string, approved: boolean): Promise<{ run: AgentRun }>
  completeCanvasAction?(
    runId: string,
    resultObjectId: string,
    baseRevision: number,
  ): Promise<{ run: AgentRun }>
  final?(runId: string, requestId: string, baseRevision: number): Promise<{ run: AgentRun }>
}

export function createAgentApi(fetchImpl: typeof fetch = (...args) => fetch(...args)): AgentApi {
  const sessionFetch =
    typeof window === 'undefined'
      ? fetchImpl
      : withSessionCsrf(fetchImpl, window.location.origin, () => document.cookie)
  async function request<T>(path: string, init?: RequestInit): Promise<T> {
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), 20000)
    try {
      const response = await sessionFetch(path, { ...init, signal: controller.signal })
      const body: unknown = await response.json().catch(() => null)
      if (!response.ok) {
        const detail = body && typeof body === 'object' ? (body as Record<string, unknown>) : {}
        throw new AgentApiError(
          typeof detail.error === 'string'
            ? detail.error
            : typeof detail.detail === 'string'
              ? detail.detail
              : `助手请求失败 HTTP ${response.status}`,
          response.status,
        )
      }
      if (!body || typeof body !== 'object') {
        throw new Error('助手响应无法读取，请查询原请求')
      }
      return body as T
    } finally {
      clearTimeout(timeout)
    }
  }
  let hostEnabled = false
  let modeReady: Promise<AgentModels> | undefined
  const hostModels = () => {
    modeReady ??= request<AgentModels>('/api/agent/models')
      .then((result) => {
        hostEnabled = result.hostEnabled === true
        return result
      })
      .catch((error: unknown) => {
        modeReady = undefined
        throw error
      })
    return modeReady
  }
  const documentPath = (id: string) =>
    `/api/documents/${encodeURIComponent(id)}/${hostEnabled ? 'host' : 'agent'}`
  return {
    models: hostModels,
    eventPath: (id) => `${documentPath(id)}/events`,
    snapshot: async (id, clientRequestId) => {
      await hostModels()
      return request<AgentSnapshot>(
        documentPath(id) +
          (clientRequestId ? `?clientRequestId=${encodeURIComponent(clientRequestId)}` : ''),
      )
    },
    create: async (id, body) => {
      await hostModels()
      return request<{ run: AgentRun; replayed?: boolean }>(
        hostEnabled ? '/api/host/turn' : `${documentPath(id)}/runs`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(
            hostEnabled
              ? {
                  clientRequestId: body.clientRequestId,
                  documentId: id,
                  message: body.message,
                  baseRevision: body.baseRevision,
                  selectedObjectIds: body.selectedObjectIds,
                  ...(body.context ? { context: body.context } : {}),
                  ...(body.supersedeRunId ? { supersedeRunId: body.supersedeRunId } : {}),
                  ...(body.clarifyOptionId ? { clarifyOptionId: body.clarifyOptionId } : {}),
                  ...(body.clarifyRunId ? { clarifyRunId: body.clarifyRunId } : {}),
                }
              : body,
          ),
        },
      )
    },
    completeCanvasAction: (id, resultObjectId, baseRevision) =>
      request(`/api/host/runs/${encodeURIComponent(id)}/canvas-result`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ resultObjectId, baseRevision }),
      }),
    get: async (id) => {
      await hostModels()
      return request(`/api/${hostEnabled ? 'host' : 'agent'}/runs/${encodeURIComponent(id)}`)
    },
    retry: (id, clientRequestId, stepId, index) =>
      request(`/api/host/runs/${encodeURIComponent(id)}/retry`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ clientRequestId, stepId, index }),
      }),
    retryBatchItems: (id, requestKey, itemIds) =>
      request(`/api/core/runs/${encodeURIComponent(id)}/batch-retry`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ request_key: requestKey, item_ids: itemIds }),
      }),
    confirm: (id, clientRequestId, approved) =>
      request(`/api/host/runs/${encodeURIComponent(id)}/confirm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ clientRequestId, approved }),
      }),
    final: (id, clientRequestId, baseRevision) =>
      request(`/api/host/runs/${encodeURIComponent(id)}/confirm-final`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ clientRequestId, baseRevision }),
      }),
    stop: async (id) => {
      await hostModels()
      return request(
        `/api/${hostEnabled ? 'host' : 'agent'}/runs/${encodeURIComponent(id)}/${hostEnabled ? 'cancel' : 'stop'}`,
        { method: 'POST' },
      )
    },
  }
}
