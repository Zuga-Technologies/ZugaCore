import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api, setToken, clearToken, ApiError } from '../api/client'

interface User {
  id: string
  email: string
  role: string
  is_admin: boolean
  name?: string | null
  avatar_url?: string | null
}

interface LoginResponse {
  token: string
  user: User
}

interface AuthConfig {
  auth_mode: string
  google_client_id: string | null
  github_client_id: string | null
  microsoft_client_id: string | null
  providers: string[]
}

export const useAuthStore = defineStore('auth', () => {
  const user = ref<User | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)
  const authConfig = ref<AuthConfig | null>(null)

  const isAuthenticated = computed(() => user.value !== null)
  const isAdmin = computed(() => user.value?.is_admin ?? false)
  const authMode = computed(() => authConfig.value?.auth_mode ?? 'dev')
  const googleClientId = computed(() => authConfig.value?.google_client_id ?? null)
  const providers = computed(() => authConfig.value?.providers ?? [])

  async function fetchAuthConfig() {
    try {
      authConfig.value = await api.get<AuthConfig>('/api/auth/config')
    } catch {
      // Default to dev mode if config endpoint fails
      authConfig.value = { auth_mode: 'dev', google_client_id: null, github_client_id: null, microsoft_client_id: null, providers: [] }
    }
  }

  const message = ref<string | null>(null)

  async function login(email: string) {
    loading.value = true
    error.value = null
    try {
      const res = await api.post<LoginResponse>('/api/auth/login', { email })
      setToken(res.token)
      user.value = res.user
    } catch (e) {
      if (e instanceof ApiError) {
        const body = e.body as Record<string, string> | undefined
        error.value = body?.detail ?? `Login failed (${e.status})`
      } else {
        error.value = 'Network error — is the backend running?'
      }
      throw e
    } finally {
      loading.value = false
    }
  }

  async function passwordLogin(email: string, password: string) {
    loading.value = true
    error.value = null
    try {
      const res = await api.post<LoginResponse>('/api/auth/password-login', { email, password })
      setToken(res.token)
      user.value = res.user
    } catch (e) {
      if (e instanceof ApiError) {
        const body = e.body as Record<string, string> | undefined
        error.value = body?.detail ?? `Login failed (${e.status})`
      } else {
        error.value = 'Network error — is the backend running?'
      }
      throw e
    } finally {
      loading.value = false
    }
  }

  async function register(email: string, password: string) {
    loading.value = true
    error.value = null
    message.value = null
    try {
      const res = await api.post<{ message: string }>('/api/auth/register', { email, password })
      message.value = res.message
    } catch (e) {
      if (e instanceof ApiError) {
        const body = e.body as Record<string, string> | undefined
        error.value = body?.detail ?? `Registration failed (${e.status})`
      } else {
        error.value = 'Network error — is the backend running?'
      }
      throw e
    } finally {
      loading.value = false
    }
  }

  async function forgotPassword(email: string) {
    loading.value = true
    error.value = null
    message.value = null
    try {
      const res = await api.post<{ message: string }>('/api/auth/forgot-password', { email })
      message.value = res.message
    } catch (e) {
      if (e instanceof ApiError) {
        const body = e.body as Record<string, string> | undefined
        error.value = body?.detail ?? `Request failed (${e.status})`
      } else {
        error.value = 'Network error — is the backend running?'
      }
      throw e
    } finally {
      loading.value = false
    }
  }

  async function resetPassword(token: string, password: string) {
    loading.value = true
    error.value = null
    message.value = null
    try {
      const res = await api.post<{ message: string }>('/api/auth/reset-password', { token, password })
      message.value = res.message
    } catch (e) {
      if (e instanceof ApiError) {
        const body = e.body as Record<string, string> | undefined
        error.value = body?.detail ?? `Reset failed (${e.status})`
      } else {
        error.value = 'Network error — is the backend running?'
      }
      throw e
    } finally {
      loading.value = false
    }
  }

  async function verifyEmail(token: string) {
    loading.value = true
    error.value = null
    message.value = null
    try {
      const res = await api.post<{ message: string }>('/api/auth/verify-email', { token })
      message.value = res.message
    } catch (e) {
      if (e instanceof ApiError) {
        const body = e.body as Record<string, string> | undefined
        error.value = body?.detail ?? `Verification failed (${e.status})`
      } else {
        error.value = 'Network error — is the backend running?'
      }
      throw e
    } finally {
      loading.value = false
    }
  }

  async function loginWithGoogle(credential: string) {
    loading.value = true
    error.value = null
    try {
      const res = await api.post<LoginResponse>('/api/auth/google', { credential })
      setToken(res.token)
      user.value = res.user
    } catch (e) {
      if (e instanceof ApiError) {
        const body = e.body as Record<string, string> | undefined
        error.value = body?.detail ?? `Google login failed (${e.status})`
      } else {
        error.value = 'Network error — is the backend running?'
      }
      throw e
    } finally {
      loading.value = false
    }
  }

  async function loginWithOAuth(provider: string, code: string, redirectUri?: string) {
    loading.value = true
    error.value = null
    try {
      const res = await api.post<LoginResponse>('/api/auth/oauth', {
        provider, code, redirect_uri: redirectUri,
      })
      setToken(res.token)
      user.value = res.user
    } catch (e) {
      if (e instanceof ApiError) {
        const body = e.body as Record<string, string> | undefined
        error.value = body?.detail ?? `${provider} login failed (${e.status})`
      } else {
        error.value = 'Network error — is the backend running?'
      }
      throw e
    } finally {
      loading.value = false
    }
  }

  function getOAuthUrl(provider: string): string {
    const base = window.location.origin
    const redirect = `${base}/auth/callback?provider=${provider}`
    const cfg = authConfig.value
    // Use server-provided client IDs (from /api/auth/config) instead of build-time VITE_ vars
    const urls: Record<string, string> = {
      microsoft: `https://login.microsoftonline.com/common/oauth2/v2.0/authorize?client_id=${cfg?.microsoft_client_id || ''}&response_type=code&redirect_uri=${encodeURIComponent(redirect)}&scope=openid+email+profile`,
      github: `https://github.com/login/oauth/authorize?client_id=${cfg?.github_client_id || ''}&redirect_uri=${encodeURIComponent(redirect)}&scope=read:user+user:email`,
      apple: `https://appleid.apple.com/auth/authorize?client_id=${import.meta.env.VITE_APPLE_CLIENT_ID || ''}&redirect_uri=${encodeURIComponent(redirect)}&response_type=code&scope=name+email&response_mode=form_post`,
    }
    return urls[provider] || ''
  }

  async function logout() {
    try {
      await api.post('/api/auth/logout')
    } catch {
      // Logout endpoint may fail if token already expired — that's fine
    }
    clearToken()
    user.value = null
  }

  async function checkAuth() {
    loading.value = true
    try {
      user.value = await api.get<User>('/api/auth/me')
    } catch {
      clearToken()
      user.value = null
    } finally {
      loading.value = false
    }
  }

  return {
    user, loading, error, message, authConfig,
    isAuthenticated, isAdmin, authMode, googleClientId, providers,
    fetchAuthConfig, login, passwordLogin, register,
    forgotPassword, resetPassword, verifyEmail,
    loginWithGoogle, loginWithOAuth, getOAuthUrl,
    logout, checkAuth,
  }
})
