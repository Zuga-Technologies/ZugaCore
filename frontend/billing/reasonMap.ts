// reasonMap.ts — maps stored token `reason` keys to a display label + studio
// color. Studio identity is encoded only in the free-text `reason` (e.g.
// "therapist", "gamer_overlay"); this is the single place that turns those
// keys into something a user sees. Unknown reasons fall back to a title-cased
// label + neutral slate.
import { formatReason } from './helpers'

export interface ReasonMeta {
  label: string
  color: string
}

export const REASON_MAP: Record<string, ReasonMeta> = {
  therapist:     { label: 'Therapist Speech', color: '#fb7185' }, // life / Spiritus
  meditation:    { label: 'Meditation',       color: '#fb7185' },
  journal:       { label: 'Journal',          color: '#fb7185' },
  gamer_overlay: { label: 'Gamer Overlay',    color: '#ec4899' }, // gamer / Ludus
  chat:          { label: 'Zugabot Chat',     color: '#a3e635' },
  admin_grant:   { label: 'Admin Grant',      color: '#a3e635' },
  welcome:       { label: 'Welcome Bonus',    color: '#a3e635' },
}

export function reasonMeta(reason: string | null): ReasonMeta {
  if (reason && REASON_MAP[reason]) return REASON_MAP[reason]
  return { label: formatReason(reason) || 'Other', color: '#64748b' }
}
