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
