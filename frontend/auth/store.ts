import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api, setToken, clearToken, ApiError } from '../api/client'

interface User {
  id: string
  email: string
  role: string
  is_admin: boolean
}

interface LoginResponse {
  token: string
  user: User
}

export const useAuthStore = defineStore('auth', () => {
  const user = ref<User | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  const isAuthenticated = computed(() => user.value !== null)
  const isAdmin = computed(() => user.value?.is_admin ?? false)

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

  return { user, loading, error, isAuthenticated, isAdmin, login, logout, checkAuth }
})
