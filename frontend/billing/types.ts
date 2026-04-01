export interface TokenBalance {
  free: number
  subscription: number
  purchased: number
  total: number
  is_unlimited: boolean
}

export interface Transaction {
  id: number
  type: string
  amount: number
  source: string | null
  reason: string | null
  balance_after: number | null
  created_at: string | null
}

export interface SubscriptionInfo {
  subscribed: boolean
  tier?: string
  status?: string
  tokens_per_cycle?: number
  current_period_end?: string
}

export interface Pack {
  id: string
  tokens: number
  price_cents: number
}

export interface Tier {
  id: string
  tokens_per_month: number
  price_cents: number
}
