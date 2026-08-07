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

// Keys read by the login surfaces (ZugaApp LoginView, ZugaLife App.vue) to show
// a "session expired" message and return the user to where they were after they
// sign back in. sessionStorage (not a query param) because Spiritus's catch-all
// route drops unknown query strings on the redirect to '/'.
export const AUTH_REASON_KEY = 'zuga_auth_reason'
export const POST_LOGIN_REDIRECT_KEY = 'zuga_post_login_redirect'

/**
 * Session is unrecoverable (401 with no token, or refresh failed). Clear it,
 * remember where the user was, and send them to login with a friendly reason.
 * Idempotent: a no-op if we're already sitting on the login screen.
 */
export function redirectToLogin(reason: 'expired' | 'required' = 'expired'): void {
  clearSession()
  if (typeof window === 'undefined') return
  const path = window.location.pathname
  if (path === '/login') return
  try {
    sessionStorage.setItem(AUTH_REASON_KEY, reason)
    // Don't loop the user back to a transient API path; remember the page.
    sessionStorage.setItem(POST_LOGIN_REDIRECT_KEY, path + window.location.search)
  } catch {
    // Private mode / storage disabled — redirect still works, just no message.
  }
  window.location.href = '/login'
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
    const doFetch = () => fetch('/api/auth/session/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refreshToken }),
    })
    try {
      let res: Response
      try {
        res = await doFetch()
      } catch (err) {
        if (!(err instanceof TypeError)) throw err
        await waitForVisible()
        res = await doFetch()
      }
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

async function rawFetch(method: string, path: string, body: unknown, token: string | null, timeoutMs?: number): Promise<Response> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (token) headers['Authorization'] = `Bearer ${token}`
  const opts: RequestInit = {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  }
  if (!timeoutMs) return fetch(path, opts)
  // AbortSignal.timeout would be simpler but isn't safe to assume everywhere
  // this bundle runs (older embedded webviews) — build it by hand.
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    return await fetch(path, { ...opts, signal: controller.signal })
  } finally {
    clearTimeout(timer)
  }
}

// Mobile browsers kill in-flight fetches when the tab is backgrounded. The
// promise rejects with TypeError ("Failed to fetch" / "network error") on
// resume. Wait until the tab is visible again, then replay once.
function waitForVisible(): Promise<void> {
  if (typeof document === 'undefined' || !document.hidden) return Promise.resolve()
  return new Promise(resolve => {
    const onVis = () => {
      if (!document.hidden) {
        document.removeEventListener('visibilitychange', onVis)
        resolve()
      }
    }
    document.addEventListener('visibilitychange', onVis)
  })
}

async function fetchWithResumeRetry(method: string, path: string, body: unknown, token: string | null, timeoutMs?: number): Promise<Response> {
  try {
    return await rawFetch(method, path, body, token, timeoutMs)
  } catch (err) {
    if (!(err instanceof TypeError)) throw err
    await waitForVisible()
    return rawFetch(method, path, body, token, timeoutMs)
  }
}

// Distinguishable from a normal ApiError so callers can offer "retry" instead
// of a generic failure message when a request was aborted client-side rather
// than rejected by the server.
export class ApiTimeoutError extends Error {
  constructor() {
    super('Request timed out')
    this.name = 'ApiTimeoutError'
  }
}

async function request<T>(method: string, path: string, body?: unknown, timeoutMs?: number): Promise<T> {
  // Don't refresh-and-retry the refresh endpoint itself — would infinite loop.
  const isRefreshCall = path === '/api/auth/session/refresh'
  let token = getToken()
  let res: Response
  try {
    res = await fetchWithResumeRetry(method, path, body, token, timeoutMs)
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw new ApiTimeoutError()
    throw err
  }

  if (res.status === 401 && !isRefreshCall) {
    // Only attempt refresh when we actually had a token to refresh. A 401 with
    // no token at all (e.g. opened on a fresh origin where no session was ever
    // stored — standalone dev server / rotating tunnel) skips straight to the
    // redirect so the user lands on login instead of silently failing writes.
    if (token) {
      const newToken = await tryRefresh()
      if (newToken) {
        // Retry once with fresh token.
        token = newToken
        res = await fetchWithResumeRetry(method, path, body, token, timeoutMs)
      }
    }
    // Still 401 (refresh failed, or no token to begin with)? Session is dead —
    // clear and bounce to login with a reason + return path so the user can
    // sign back in and land where they were instead of silently failing.
    // Exception: /api/auth/me is a speculative "am I logged in?" probe (the
    // router's one-shot hydration check fires it on EVERY page load, public
    // or not). A 401 from it just means "no" — not a session that died mid-
    // use. Hard-redirecting here was firing on every anonymous visit to any
    // public page (landing, login, register) before the router guard ever
    // got a chance to route them correctly. Found 2026-08-06: it silently
    // made the marketing landing page unreachable for anyone logged out.
    if (res.status === 401 && path !== '/api/auth/me') {
      redirectToLogin(token ? 'expired' : 'required')
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
  get: <T>(path: string, timeoutMs?: number) => request<T>('GET', path, undefined, timeoutMs),
  post: <T>(path: string, body?: unknown, timeoutMs?: number) => request<T>('POST', path, body, timeoutMs),
  put: <T>(path: string, body?: unknown, timeoutMs?: number) => request<T>('PUT', path, body, timeoutMs),
  patch: <T>(path: string, body?: unknown, timeoutMs?: number) => request<T>('PATCH', path, body, timeoutMs),
  delete: <T>(path: string, timeoutMs?: number) => request<T>('DELETE', path, undefined, timeoutMs),
}
