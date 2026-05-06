const TOKEN_KEY = 'zugaapp_token'
const REFRESH_KEY = 'zugaapp_refresh'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_KEY)
}

export function setRefreshToken(token: string | null | undefined): void {
  if (token) localStorage.setItem(REFRESH_KEY, token)
  else localStorage.removeItem(REFRESH_KEY)
}

export function clearRefreshToken(): void {
  localStorage.removeItem(REFRESH_KEY)
}

export function setSession(token: string, refreshToken?: string | null): void {
  setToken(token)
  if (refreshToken !== undefined) setRefreshToken(refreshToken)
}

export function clearSession(): void {
  clearToken()
  clearRefreshToken()
}

// Single in-flight refresh — many concurrent calls hit 401 at the same time
// when the access token expires; we want exactly one /session/refresh call
// out, with everyone awaiting the same result.
let inFlightRefresh: Promise<string | null> | null = null

/**
 * Attempt to mint a new access token using the stored refresh token.
 * Returns the new access token on success (already persisted), or null on
 * failure (refresh token missing, expired, or rotated by another tab).
 *
 * Concurrent callers share one refresh request — important when a page
 * fires a dozen API calls on mount and they all 401 in parallel.
 */
export async function tryRefresh(): Promise<string | null> {
  if (inFlightRefresh) return inFlightRefresh
  const refreshToken = getRefreshToken()
  if (!refreshToken) return null

  inFlightRefresh = (async () => {
    try {
      const res = await fetch('/api/auth/session/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refreshToken }),
      })
      if (!res.ok) return null
      const body = await res.json() as { token: string; refresh_token: string }
      setSession(body.token, body.refresh_token)
      return body.token
    } catch {
      return null
    } finally {
      // Clear the in-flight gate on the next tick so any 401 retries that
      // started just before the refresh resolved get the fresh token.
      setTimeout(() => { inFlightRefresh = null }, 0)
    }
  })()

  return inFlightRefresh
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public body: unknown,
  ) {
    const detail =
      body && typeof body === 'object' && 'detail' in body
        ? String((body as { detail: unknown }).detail)
        : null
    super(detail || `Request failed (${status})`)
    this.name = 'ApiError'
  }
}

async function rawFetch(method: string, path: string, body: unknown, token: string | null): Promise<Response> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (token) headers['Authorization'] = `Bearer ${token}`
  return fetch(path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  // Don't refresh-and-retry the refresh endpoint itself — would infinite loop.
  const isRefreshCall = path === '/api/auth/session/refresh'
  let token = getToken()
  let res = await rawFetch(method, path, body, token)

  if (res.status === 401 && token && !isRefreshCall) {
    const newToken = await tryRefresh()
    if (newToken) {
      // Retry once with fresh token.
      token = newToken
      res = await rawFetch(method, path, body, token)
    }
    // Still 401 after refresh? Session is genuinely dead — clear and redirect.
    if (res.status === 401) {
      clearSession()
      if (window.location.pathname !== '/login') {
        window.location.href = '/login'
      }
    }
  }

  if (!res.ok) {
    const text = await res.text()
    let parsed: unknown
    try { parsed = JSON.parse(text) } catch { parsed = text }
    throw new ApiError(res.status, parsed)
  }

  if (res.status === 204 || res.headers.get('content-length') === '0') {
    return undefined as T
  }
  return res.json() as Promise<T>
}

export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body),
  put: <T>(path: string, body?: unknown) => request<T>('PUT', path, body),
  patch: <T>(path: string, body?: unknown) => request<T>('PATCH', path, body),
  delete: <T>(path: string) => request<T>('DELETE', path),
}
