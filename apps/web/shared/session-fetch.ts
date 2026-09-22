/** Same-origin mutation protection; credentials never follow an external request. */
export function withSessionCsrf(
  fetchImpl: typeof fetch,
  origin: string,
  readCookie: () => string,
): typeof fetch {
  return (input, init) => {
    const request = input instanceof Request ? input : null
    const url = new URL(request?.url ?? String(input), origin)
    const method = (init?.method ?? request?.method ?? 'GET').toUpperCase()
    if (url.origin !== origin || ['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      return fetchImpl(input, init)
    }
    const headers = new Headers(init?.headers ?? request?.headers)
    const token = readCookie()
      .split(';')
      .map((part) => part.trim())
      .find((part) => part.startsWith('__Host-aivideo-csrf='))
      ?.slice('__Host-aivideo-csrf='.length)
    if (token) {
      headers.set('X-CSRF-Token', token)
    }
    return fetchImpl(input, { ...init, headers })
  }
}

let installed = false
export function installSessionFetch(): void {
  if (installed || typeof window === 'undefined') {
    return
  }
  installed = true
  window.fetch = withSessionCsrf(
    window.fetch.bind(window),
    window.location.origin,
    () => document.cookie,
  )
}
