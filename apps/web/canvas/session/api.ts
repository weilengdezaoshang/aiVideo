import { withSessionCsrf } from '../../shared/session-fetch.js'
import type { SessionApi, SessionSnapshot } from './store.js'

export function createSessionApi(
  options: {
    fetch?: typeof fetch
    requestTimeoutMs?: number
    language?: string
    switchPollIntervalMs?: number
    switchWaitMs?: number
  } = {},
): SessionApi {
  const fetchImpl = options.fetch ?? ((...args: Parameters<typeof fetch>) => fetch(...args))
  const sessionFetch =
    typeof window === 'undefined'
      ? fetchImpl
      : withSessionCsrf(fetchImpl, window.location.origin, () => document.cookie)
  const timeoutMs = options.requestTimeoutMs ?? 20000
  async function request<T>(path: string, body?: unknown): Promise<T> {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), timeoutMs)
    try {
      const response = await sessionFetch(path, {
        method: body === undefined ? 'GET' : 'POST',
        headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      })
      const result: unknown = await response.json()
      if (!response.ok) {
        const detail =
          result && typeof result === 'object' ? (result as Record<string, unknown>).detail : null
        throw new Error(
          typeof detail === 'string' ? detail : `会话请求失败 HTTP ${response.status}`,
        )
      }
      return result as T
    } finally {
      clearTimeout(timer)
    }
  }
  const path = (id: string) => `/api/core/sessions/${encodeURIComponent(id)}`
  return {
    open: (documentId) =>
      request<SessionSnapshot>('/api/core/sessions', {
        document_id: documentId,
        language: options.language,
      }),
    snapshot: (id) => request<SessionSnapshot>(path(id)),
    send: (id, body) => request(`${path(id)}/messages`, body),
    events: (id, cursor) => `${path(id)}/events?after=${cursor}`,
    heartbeat: (id, clientId, cursor, online) =>
      request(`${path(id)}/clients`, { client_id: clientId, cursor, online }),
    readNotification: (id) => request(`/api/core/notifications/${encodeURIComponent(id)}/read`, {}),
    artifacts: (id) => request(`${path(id)}/artifacts`),
    versions: async (id) =>
      (
        await request<{ versions: import('./version-history.js').AssetVersion[] }>(
          `/api/core/assets/${encodeURIComponent(id)}/versions`,
        )
      ).versions,
    switchVersion: async (assetId, versionId, body) => {
      const accepted = await request<{ command_id: string }>(
        `/api/core/assets/${encodeURIComponent(assetId)}/versions/${encodeURIComponent(versionId)}/switch`,
        body,
      )
      const started = performance.now()
      while (performance.now() - started < (options.switchWaitMs ?? 30000)) {
        const result = await request<{ status: string; error_code?: string }>(
          `/api/core/version-switches/${encodeURIComponent(accepted.command_id)}`,
        )
        if (result.status === 'applied') {
          return
        }
        if (result.status === 'conflict') {
          throw new Error('画布已更新，请同步后重试切换')
        }
        if (result.status === 'rejected') {
          throw new Error('此版本当前无法切换')
        }
        await new Promise<void>((resolve) =>
          setTimeout(resolve, options.switchPollIntervalMs ?? 500),
        )
      }
      throw new Error('版本切换仍在处理中，完成后会自动同步')
    },
  }
}
