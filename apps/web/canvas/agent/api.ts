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
  create(documentId: string, request: AgentRequest): Promise<{ run: AgentRun }>
  get(runId: string): Promise<{ run: AgentRun }>
  stop(runId: string): Promise<{ run: AgentRun }>
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
  const documentPath = (id: string) => `/api/documents/${encodeURIComponent(id)}/agent`
  return {
    models: () => request('/api/agent/models'),
    snapshot: (id, clientRequestId) =>
      request(
        documentPath(id) +
          (clientRequestId ? `?clientRequestId=${encodeURIComponent(clientRequestId)}` : ''),
      ),
    create: (id, body) =>
      request(`${documentPath(id)}/runs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }),
    get: (id) => request(`/api/agent/runs/${encodeURIComponent(id)}`),
    stop: (id) => request(`/api/agent/runs/${encodeURIComponent(id)}/stop`, { method: 'POST' }),
  }
}
