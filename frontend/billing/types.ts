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

export interface UsageBucket {
  tokens: number
  cost_usd: number
  calls: number
}

export interface UsageSummary {
  user_id: string
  period_days: number
  total_tokens: number
  total_usd: number
  total_calls: number
  by_service: Record<string, UsageBucket>
  by_reason: Record<string, UsageBucket>
}

export interface HistoryFilter {
  type: string | null   // transaction type: spend|purchase|subscription|grant|refund
  days: number | null   // trailing window
}
