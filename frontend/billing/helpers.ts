// Token economics constants — must match ZugaCore/credits/manager.py
const ZUGATOKENS_PER_DOLLAR = 100
const MARKUP_MULTIPLIER = 3

/** Convert raw USD cost to ZugaToken amount (with 3x markup). */
export function dollarsToTokens(usd: number): number {
  return Math.ceil(usd * MARKUP_MULTIPLIER * ZUGATOKENS_PER_DOLLAR)
}

/** Format a raw USD cost as a token display string, e.g. "15 tokens". */
export function formatTokenCost(usd: number): string {
  const tokens = dollarsToTokens(usd)
  return `${tokens} token${tokens !== 1 ? 's' : ''}`
}

export const txTypeLabel: Record<string, string> = {
  spend: 'Used',
  purchase: 'Purchased',
  free_refill: 'Daily Refill',
  subscription: 'Subscription',
  grant: 'Bonus',
  expire: 'Expired',
  refund: 'Refund',
}

/** Map transaction type to lucide icon name (consumer resolves the component). */
export const txTypeIconName: Record<string, string> = {
  spend: 'TrendingDown',
  purchase: 'ShoppingCart',
  free_refill: 'RefreshCw',
  subscription: 'CreditCard',
  grant: 'Gift',
  expire: 'Clock',
  refund: 'TrendingUp',
}

export function formatDate(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  const now = new Date()
  const isToday = d.toDateString() === now.toDateString()
  if (isToday) return 'Today'
  const yesterday = new Date(now)
  yesterday.setDate(yesterday.getDate() - 1)
  if (d.toDateString() === yesterday.toDateString()) return 'Yesterday'
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

export function formatReason(reason: string | null): string {
  if (!reason) return ''
  return reason.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
}
