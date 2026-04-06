import { ref, computed } from 'vue'
import { defineStore } from 'pinia'
import { api } from '../api/client'
import type { TokenBalance, Transaction, SubscriptionInfo, Pack, Tier } from './types'

export const useTokenStore = defineStore('zugatokens', () => {
  // ── State ────────────────────────────────────────────────────────
  const balance = ref<TokenBalance | null>(null)
  const transactions = ref<Transaction[]>([])
  const subscription = ref<SubscriptionInfo>({ subscribed: false })
  const packs = ref<Pack[]>([])
  const tiers = ref<Tier[]>([])
  const loading = ref(false)
  const purchaseLoading = ref<string | null>(null)
  const purchaseError = ref<string | null>(null)

  // ── Computed ─────────────────────────────────────────────────────
  const balancePercent = computed(() => {
    if (!balance.value || balance.value.total === 0) return { free: 0, sub: 0, purchased: 0 }
    const t = balance.value.total
    return {
      free: (balance.value.free / t) * 100,
      sub: (balance.value.subscription / t) * 100,
      purchased: (balance.value.purchased / t) * 100,
    }
  })

  const hasBalance = computed(() => {
    if (!balance.value) return false
    return balance.value.is_unlimited || balance.value.total > 0
  })

  // ── Actions ──────────────────────────────────────────────────────
  function _notify() {
    window.dispatchEvent(new CustomEvent('zugatokens-updated'))
  }

  async function fetchBalance() {
    try {
      balance.value = await api.get<TokenBalance>('/api/tokens/balance')
    } catch (e) {
      console.error('Failed to fetch token balance:', e)
    }
  }

  async function fetchAll() {
    loading.value = true
    try {
      const [bal, txHistory, sub] = await Promise.all([
        api.get<TokenBalance>('/api/tokens/balance'),
        api.get<{ transactions: Transaction[] }>('/api/tokens/history?limit=30'),
        api.get<SubscriptionInfo>('/api/tokens/subscription'),
      ])
      balance.value = bal
      transactions.value = txHistory.transactions
      subscription.value = sub
    } catch (e) {
      console.error('Failed to load token data:', e)
    } finally {
      loading.value = false
    }
  }

  async function loadPacks() {
    try {
      const data = await api.get<{ topup_packs: Pack[]; subscription_tiers: Tier[] }>('/api/tokens/packs')
      packs.value = data.topup_packs
      tiers.value = data.subscription_tiers
    } catch (e) {
      console.error('Failed to load packs:', e)
    }
  }

  async function buyPack(packId: string) {
    purchaseLoading.value = packId
    purchaseError.value = null
    try {
      const { checkout_url } = await api.post<{ checkout_url: string }>('/api/tokens/purchase', { pack: packId })
      window.location.href = checkout_url
    } catch (e: any) {
      console.error('Purchase failed:', e)
      purchaseError.value = e?.body?.detail || e?.message || 'Purchase failed. Please try again.'
      purchaseLoading.value = null
    }
  }

  async function subscribeTier(tierId: string) {
    purchaseLoading.value = tierId
    purchaseError.value = null
    try {
      const { checkout_url } = await api.post<{ checkout_url: string }>('/api/tokens/subscribe', { tier: tierId })
      window.location.href = checkout_url
    } catch (e: any) {
      console.error('Subscribe failed:', e)
      purchaseError.value = e?.body?.detail || e?.message || 'Subscription failed. Please try again.'
      purchaseLoading.value = null
    }
  }

  async function cancelSubscription() {
    if (!confirm('Cancel your subscription? You\'ll keep remaining tokens until the end of your billing period.')) return
    try {
      await api.post('/api/tokens/cancel-subscription')
      await fetchAll()
      _notify()
    } catch (e) {
      console.error('Cancel failed:', e)
    }
  }

  async function refresh() {
    await fetchAll()
    _notify()
  }

  return {
    // State
    balance,
    transactions,
    subscription,
    packs,
    tiers,
    loading,
    purchaseLoading,
    purchaseError,
    // Computed
    balancePercent,
    hasBalance,
    // Actions
    fetchBalance,
    fetchAll,
    loadPacks,
    buyPack,
    subscribeTier,
    cancelSubscription,
    refresh,
  }
})
