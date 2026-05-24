<script setup lang="ts">
import { onMounted, ref, computed, watch, nextTick } from 'vue'
import { loadStripe, type Stripe, type StripeCardElement } from '@stripe/stripe-js'
import { useTokenStore } from './useTokens'
import { txTypeLabel, formatDate } from './helpers'
import { reasonMeta } from './reasonMap'
import {
  Coins, ShoppingCart, CreditCard, TrendingDown, TrendingUp,
  Gift, Zap, Clock, Infinity, X, Download, PieChart, ShieldCheck,
} from 'lucide-vue-next'

const props = withDefaults(defineProps<{
  tokenLabel?: string
  showHistory?: boolean
}>(), {
  tokenLabel: 'ZugaTokens',
  showHistory: true,
})

const store = useTokenStore()
const showPurchaseModal = ref(false)

const txTypeIcon: Record<string, any> = {
  spend: TrendingDown,
  purchase: ShoppingCart,
  welcome_grant: Gift,
  subscription: CreditCard,
  grant: Gift,
  expire: Clock,
  refund: TrendingUp,
}

function openPurchase() {
  store.loadPacks()
  showPurchaseModal.value = true
}

// ── Usage breakdown ("where your tokens go") ────────────────────────────
const usageDays = ref(30)
const RANGE_OPTIONS = [
  { label: '7d', days: 7 },
  { label: '30d', days: 30 },
  { label: '90d', days: 90 },
]

/** by_reason map → sorted rows with a relative bar width. */
const usageRows = computed(() => {
  const by = store.usage?.by_reason
  if (!by) return [] as Array<{ key: string; label: string; color: string; tokens: number; pct: number }>
  const rows = Object.entries(by)
    .map(([key, b]) => ({ key, ...reasonMeta(key), tokens: Math.round(b.tokens) }))
    .filter(r => r.tokens > 0)
    .sort((a, b) => b.tokens - a.tokens)
  const max = rows.length ? rows[0].tokens : 1
  return rows.map(r => ({ ...r, pct: max > 0 ? (r.tokens / max) * 100 : 0 }))
})

function setUsageDays(days: number) {
  usageDays.value = days
  store.fetchUsage(days)
}

// ── History filter + CSV export ─────────────────────────────────────────
const TYPE_OPTIONS = [
  { label: 'All activity', value: '' },
  { label: 'Used', value: 'spend' },
  { label: 'Purchased', value: 'purchase' },
  { label: 'Subscription', value: 'subscription' },
  { label: 'Bonus / Grant', value: 'grant' },
  { label: 'Refund', value: 'refund' },
]
const filterType = ref('')
const filterDays = ref(0)

function applyFilters() {
  store.fetchHistory({
    type: filterType.value || null,
    days: filterDays.value || null,
  })
}

function exportCsv() {
  const rows = store.transactions
  const header = ['Date', 'Type', 'Reason', 'Amount', 'Balance after']
  const esc = (v: string) => `"${String(v).replace(/"/g, '""')}"`
  const lines = rows.map(tx => [
    tx.created_at || '',
    txTypeLabel[tx.type] || tx.type,
    reasonMeta(tx.reason).label,
    String(Math.round(tx.amount)),
    tx.balance_after != null ? String(Math.round(tx.balance_after)) : '',
  ].map(esc).join(','))
  const csv = [header.map(esc).join(','), ...lines].join('\r\n')
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `zugatokens-history-${new Date().toISOString().slice(0, 10)}.csv`
  a.click()
  URL.revokeObjectURL(url)
}

// ── Monthly spending cap ────────────────────────────────────────────────
const capInput = ref<number | null>(null)
const capSaving = ref(false)
watch(() => store.spendingCap, (c) => { capInput.value = c?.cap_tokens ?? null }, { immediate: true })

const capActive = computed(() => !!store.spendingCap?.cap_tokens)
const capPct = computed(() => {
  const c = store.spendingCap
  if (!c || !c.cap_tokens) return 0
  return Math.min(100, (c.spent_this_period / c.cap_tokens) * 100)
})

async function saveCap() {
  capSaving.value = true
  try {
    await store.setCap(capInput.value && capInput.value > 0 ? Math.round(capInput.value) : null)
  } finally { capSaving.value = false }
}
async function removeCap() {
  capSaving.value = true
  try { await store.setCap(null); capInput.value = null }
  finally { capSaving.value = false }
}

// ── Auto top-up (opt-in; only shown when server flag is on) ─────────────
const TOPUP_PACK_OPTIONS = [
  { value: 'starter', label: '200 tokens' },
  { value: 'standard', label: '550 tokens' },
  { value: 'best_value', label: '1,200 tokens' },
  { value: 'bulk', label: '3,500 tokens' },
]
const atEnabled = ref(false)
const atThreshold = ref<number | null>(null)
const atPack = ref('standard')
const atSaving = ref(false)
watch(() => store.autotopup, (a) => {
  atEnabled.value = !!a.enabled
  atThreshold.value = a.threshold ?? null
  atPack.value = a.pack || 'standard'
}, { immediate: true, deep: true })

async function saveAutotopup() {
  atSaving.value = true
  try {
    await store.setAutotopup({
      enabled: atEnabled.value,
      threshold: atThreshold.value && atThreshold.value > 0 ? Math.round(atThreshold.value) : null,
      pack: atPack.value,
    })
  } finally { atSaving.value = false }
}

// Stripe Elements card capture for the SetupIntent
let stripe: Stripe | null = null
let cardEl: StripeCardElement | null = null
const cardFormOpen = ref(false)
const cardMountRef = ref<HTMLElement | null>(null)
const cardError = ref('')
const cardSaving = ref(false)

async function openCardForm() {
  cardError.value = ''
  const pk = (import.meta as any).env?.VITE_STRIPE_PUBLISHABLE_KEY
  if (!pk) { cardError.value = 'Stripe is not configured.'; return }
  if (!stripe) stripe = await loadStripe(pk)
  if (!stripe) { cardError.value = 'Could not load Stripe.'; return }
  cardFormOpen.value = true
  await nextTick()
  const elements = stripe.elements()
  cardEl = elements.create('card', { style: { base: { color: '#e7e5e4', fontSize: '15px', '::placeholder': { color: '#737373' } } } })
  if (cardMountRef.value) cardEl.mount(cardMountRef.value)
}

function closeCardForm() {
  cardEl?.unmount()
  cardEl = null
  cardFormOpen.value = false
}

async function submitCard() {
  if (!stripe || !cardEl) return
  cardSaving.value = true
  cardError.value = ''
  try {
    const { client_secret } = await store.createSetupIntent()
    const { error } = await stripe.confirmCardSetup(client_secret, { payment_method: { card: cardEl } })
    if (error) { cardError.value = error.message || 'Card setup failed.'; return }
    closeCardForm()
    // The webhook saves the card a moment later — refresh shortly after.
    setTimeout(() => store.fetchAutotopup(), 2000)
  } catch (e: any) {
    cardError.value = e?.body?.detail || 'Could not save card.'
  } finally { cardSaving.value = false }
}

onMounted(() => store.fetchAll())
</script>

<template>
  <!-- ── Loading skeleton ──────────────────────────────────────────────── -->
  <div v-if="store.loading" class="bp-skeleton-stack">
    <div class="bp-skeleton bp-skeleton-hero" />
    <div class="bp-skeleton bp-skeleton-history" />
  </div>

  <template v-else-if="store.balance">

    <!-- ── Balance hero ────────────────────────────────────────────────── -->
    <section class="bp-hero">
      <div class="bp-hero-edge" aria-hidden="true" />
      <div class="bp-hero-glow" aria-hidden="true" />
      <div class="bp-hero-row">
        <div class="bp-hero-block">
          <p class="bp-hero-label">Available balance</p>
          <p class="bp-hero-num">
            <template v-if="store.balance.is_unlimited">
              <Infinity :size="56" :stroke-width="2" />
            </template>
            <template v-else>
              {{ Math.round(store.balance.total).toLocaleString() }}
            </template>
          </p>
          <p class="bp-hero-token-label">{{ tokenLabel }}</p>
        </div>
        <button
          v-if="!store.balance.is_unlimited"
          @click="openPurchase"
          class="bp-cta-primary"
        >
          <Zap :size="16" :stroke-width="2.4" />
          Buy tokens
        </button>
      </div>

      <!-- Breakdown bar — multi-shade lime, all tokens are "the same currency" -->
      <template v-if="!store.balance.is_unlimited && store.balance.total > 0">
        <div class="bp-bar">
          <div
            class="bp-bar-segment bp-bar-free"
            :style="{ width: store.balancePercent.free + '%' }"
            :title="`Free: ${Math.round(store.balance.free)}`"
          />
          <div
            class="bp-bar-segment bp-bar-sub"
            :style="{ width: store.balancePercent.sub + '%' }"
            :title="`Subscription: ${Math.round(store.balance.subscription)}`"
          />
          <div
            class="bp-bar-segment bp-bar-purchased"
            :style="{ width: store.balancePercent.purchased + '%' }"
            :title="`Purchased: ${Math.round(store.balance.purchased)}`"
          />
        </div>
        <div class="bp-breakdown">
          <span class="bp-breakdown-item">
            <span class="bp-breakdown-dot bp-dot-free" />
            Free
            <span class="bp-breakdown-num">{{ Math.round(store.balance.free).toLocaleString() }}</span>
          </span>
          <span class="bp-breakdown-item">
            <span class="bp-breakdown-dot bp-dot-sub" />
            Subscription
            <span class="bp-breakdown-num">{{ Math.round(store.balance.subscription).toLocaleString() }}</span>
          </span>
          <span class="bp-breakdown-item">
            <span class="bp-breakdown-dot bp-dot-purchased" />
            Purchased
            <span class="bp-breakdown-num">{{ Math.round(store.balance.purchased).toLocaleString() }}</span>
          </span>
        </div>
      </template>
    </section>

    <!-- ── Subscription card ────────────────────────────────────────────── -->
    <section v-if="store.subscription.subscribed" class="bp-card bp-subscription">
      <div class="bp-sub-row">
        <div class="bp-sub-block">
          <p class="bp-card-label">Current plan</p>
          <p class="bp-sub-tier">{{ store.subscription.tier }}</p>
          <p class="bp-sub-meta">
            {{ store.subscription.tokens_per_cycle?.toLocaleString() }} tokens / month
            <template v-if="store.subscription.status === 'cancelled' || store.subscription.status === 'cancelling'">
              · <span class="bp-sub-cancelling">cancels {{ store.subscription.current_period_end ? new Date(store.subscription.current_period_end).toLocaleDateString() : 'soon' }}</span>
            </template>
          </p>
        </div>
        <div class="bp-sub-actions">
          <button
            @click="store.openBillingPortal()"
            class="bp-cta-ghost"
            title="Manage payment, view invoices, or cancel"
          >
            Manage billing
          </button>
          <button
            v-if="store.subscription.status === 'active'"
            @click="store.cancelSubscription()"
            class="bp-cta-ghost"
          >
            Cancel plan
          </button>
        </div>
      </div>
    </section>

    <!-- ── Desktop dashboard: history (left, wide) + controls (right) ────── -->
    <div v-if="showHistory" class="bp-cols">
    <div class="bp-col-side">

    <!-- ── Monthly spending cap ─────────────────────────────────────────── -->
    <section v-if="!store.balance.is_unlimited" class="bp-card bp-cap">
      <div class="bp-cap-inner">
        <div class="bp-cap-head">
          <div class="bp-cap-title-row">
            <ShieldCheck :size="16" :stroke-width="2" class="bp-cap-icon" />
            <div>
              <p class="bp-card-label">Monthly spending cap</p>
              <p class="bp-cap-hint">Auto-blocks spending past this many tokens each 30 days. Leave blank for no cap.</p>
            </div>
          </div>
        </div>
        <div class="bp-cap-controls">
          <input
            v-model.number="capInput"
            type="number"
            min="0"
            step="50"
            inputmode="numeric"
            placeholder="No cap"
            class="bp-cap-input"
            aria-label="Monthly token cap"
          />
          <button class="bp-cap-save" :disabled="capSaving" @click="saveCap">Save</button>
          <button v-if="capActive" class="bp-cap-clear" :disabled="capSaving" @click="removeCap">Remove</button>
        </div>
        <div v-if="capActive" class="bp-cap-progress">
          <div class="bp-cap-track">
            <div class="bp-cap-fill" :class="{ 'bp-cap-fill-warn': capPct >= 80 }" :style="{ width: capPct + '%' }" />
          </div>
          <p class="bp-cap-meta">
            {{ Math.round(store.spendingCap!.spent_this_period).toLocaleString() }}
            / {{ Math.round(store.spendingCap!.cap_tokens!).toLocaleString() }} tokens this period
          </p>
        </div>
      </div>
    </section>

    <!-- ── Auto top-up (only when server flag is on) ────────────────────── -->
    <section v-if="store.autotopup.available && !store.balance.is_unlimited" class="bp-card bp-cap">
      <div class="bp-cap-inner">
        <div class="bp-cap-title-row">
          <Zap :size="16" :stroke-width="2.4" class="bp-cap-icon" />
          <div>
            <p class="bp-card-label">Auto top-up</p>
            <p class="bp-cap-hint">Automatically buy more tokens when your balance runs low, so you never get interrupted mid-task.</p>
          </div>
        </div>

        <!-- Saved card / add card -->
        <div class="bp-at-card-row">
          <template v-if="store.autotopup.card?.last4">
            <CreditCard :size="15" :stroke-width="2" />
            <span class="bp-at-card-text">{{ store.autotopup.card.brand }} •••• {{ store.autotopup.card.last4 }}</span>
            <button class="bp-cap-clear" @click="openCardForm">Replace card</button>
          </template>
          <template v-else>
            <span class="bp-at-card-text bp-at-muted">No card on file</span>
            <button class="bp-cap-save" @click="openCardForm">Add a card</button>
          </template>
        </div>

        <!-- Card capture form -->
        <div v-if="cardFormOpen" class="bp-at-cardform">
          <div ref="cardMountRef" class="bp-at-card-element" />
          <div class="bp-at-cardform-actions">
            <button class="bp-cap-save" :disabled="cardSaving" @click="submitCard">{{ cardSaving ? 'Saving…' : 'Save card' }}</button>
            <button class="bp-cap-clear" :disabled="cardSaving" @click="closeCardForm">Cancel</button>
          </div>
          <p v-if="cardError" class="bp-at-error">{{ cardError }}</p>
        </div>

        <!-- Settings (only meaningful once a card is saved) -->
        <div class="bp-at-settings" :class="{ 'bp-at-disabled': !store.autotopup.has_card }">
          <label class="bp-at-toggle">
            <input type="checkbox" v-model="atEnabled" :disabled="!store.autotopup.has_card" />
            <span>Enable auto top-up</span>
          </label>
          <div class="bp-at-fields">
            <label class="bp-at-field">
              <span>When balance falls below</span>
              <input v-model.number="atThreshold" type="number" min="0" step="50" placeholder="100" class="bp-cap-input" :disabled="!store.autotopup.has_card" />
            </label>
            <label class="bp-at-field">
              <span>buy</span>
              <select v-model="atPack" class="bp-filter-select" :disabled="!store.autotopup.has_card">
                <option v-for="o in TOPUP_PACK_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
              </select>
            </label>
          </div>
          <button class="bp-cap-save" :disabled="atSaving || !store.autotopup.has_card" @click="saveAutotopup">Save</button>
        </div>
      </div>
    </section>

    <!-- ── Usage breakdown — where your tokens go ───────────────────────── -->
    <section v-if="usageRows.length" class="bp-card bp-usage">
      <header class="bp-card-header">
        <h2 class="bp-card-title">
          <PieChart :size="15" :stroke-width="2" class="bp-card-title-icon" />
          Where your tokens go
        </h2>
        <div class="bp-range">
          <button
            v-for="opt in RANGE_OPTIONS"
            :key="opt.days"
            class="bp-range-btn"
            :class="{ 'bp-range-active': usageDays === opt.days }"
            @click="setUsageDays(opt.days)"
          >{{ opt.label }}</button>
        </div>
      </header>
      <ul class="bp-usage-list">
        <li v-for="row in usageRows" :key="row.key" class="bp-usage-row">
          <div class="bp-usage-head">
            <span class="bp-usage-dot" :style="{ background: row.color }" />
            <span class="bp-usage-label">{{ row.label }}</span>
            <span class="bp-usage-num">{{ row.tokens.toLocaleString() }}</span>
          </div>
          <div class="bp-usage-track">
            <div class="bp-usage-fill" :style="{ width: row.pct + '%', background: row.color }" />
          </div>
        </li>
      </ul>
    </section>

    </div><!-- /bp-col-side -->

    <div class="bp-col-main">
    <!-- ── Transaction history ──────────────────────────────────────────── -->
    <section class="bp-card bp-history">
      <header class="bp-card-header bp-history-header">
        <h2 class="bp-card-title">Recent activity</h2>
        <div class="bp-history-controls">
          <select v-model="filterType" class="bp-filter-select" aria-label="Filter by type" @change="applyFilters">
            <option v-for="o in TYPE_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
          </select>
          <select v-model.number="filterDays" class="bp-filter-select" aria-label="Filter by date" @change="applyFilters">
            <option :value="0">All time</option>
            <option :value="30">Last 30 days</option>
            <option :value="90">Last 90 days</option>
            <option :value="365">Last year</option>
          </select>
          <button
            class="bp-export-btn"
            :disabled="!store.transactions.length"
            title="Export visible activity as CSV"
            @click="exportCsv"
          >
            <Download :size="13" :stroke-width="2" />
            CSV
          </button>
        </div>
      </header>

      <div v-if="store.transactions.length === 0" class="bp-empty">
        <Coins :size="28" :stroke-width="1.5" />
        <p>No token activity yet.</p>
        <p class="bp-empty-hint">Your purchases, grants, and spends will appear here.</p>
      </div>

      <ul v-else class="bp-tx-list">
        <li
          v-for="tx in store.transactions"
          :key="tx.id"
          class="bp-tx-row"
        >
          <div
            class="bp-tx-icon"
            :class="tx.amount > 0 ? 'bp-tx-icon-pos' : 'bp-tx-icon-neg'"
          >
            <component :is="txTypeIcon[tx.type] || Coins" :size="14" :stroke-width="2" />
          </div>
          <div class="bp-tx-body">
            <p class="bp-tx-label">{{ txTypeLabel[tx.type] || tx.type }}</p>
            <p v-if="tx.reason" class="bp-tx-reason">
              <span class="bp-tx-reason-dot" :style="{ background: reasonMeta(tx.reason).color }" />
              {{ reasonMeta(tx.reason).label }}
            </p>
          </div>
          <div class="bp-tx-amount-block">
            <p
              class="bp-tx-amount"
              :class="tx.amount > 0 ? 'bp-tx-amount-pos' : 'bp-tx-amount-neg'"
            >
              {{ tx.amount > 0 ? '+' : '' }}{{ Math.round(tx.amount).toLocaleString() }}
            </p>
            <p class="bp-tx-date">{{ formatDate(tx.created_at) }}</p>
          </div>
        </li>
      </ul>
    </section>
    </div><!-- /bp-col-main -->
    </div><!-- /bp-cols -->

  </template>

  <!-- ═══════════════════════════════════════════════════
       PURCHASE MODAL — AuthLayout DNA (corners + edge + glow)
  ════════════════════════════════════════════════════ -->
  <teleport to="body">
    <transition name="bp-modal">
      <div
        v-if="showPurchaseModal"
        class="bp-modal-backdrop"
        @click.self="showPurchaseModal = false"
      >
        <div class="bp-modal">
          <!-- Chrome -->
          <div class="bp-modal-edge" aria-hidden="true" />
          <div class="bp-modal-corner tl" aria-hidden="true" />
          <div class="bp-modal-corner tr" aria-hidden="true" />
          <div class="bp-modal-corner bl" aria-hidden="true" />
          <div class="bp-modal-corner br" aria-hidden="true" />

          <!-- Close (top-right, inside chrome) -->
          <button
            @click="showPurchaseModal = false"
            class="bp-modal-close"
            aria-label="Close"
          >
            <X :size="18" :stroke-width="2" />
          </button>

          <!-- Header -->
          <div class="bp-modal-header">
            <div class="bp-modal-icon">
              <Coins :size="20" :stroke-width="2" />
            </div>
            <div>
              <h2 class="bp-modal-title">Buy {{ tokenLabel }}</h2>
              <p class="bp-modal-sub">Top-up packs · tokens never expire.</p>
            </div>
          </div>

          <!-- Token packs as pressable tiles -->
          <div class="bp-section">
            <p class="bp-section-label">One-time packs</p>
            <div class="bp-pack-list">
              <button
                v-for="pack in store.packs"
                :key="pack.id"
                @click="store.buyPack(pack.id)"
                :disabled="store.purchaseLoading !== null"
                class="bp-pack"
                :class="{ 'bp-pack-featured': pack.id === 'best_value' }"
              >
                <span
                  v-if="pack.id === 'best_value'"
                  class="bp-pack-badge"
                >Best value</span>
                <div class="bp-pack-tokens">
                  <span class="bp-pack-tokens-num">{{ pack.tokens.toLocaleString() }}</span>
                  <span class="bp-pack-tokens-label">tokens</span>
                </div>
                <div class="bp-pack-price">${{ (pack.price_cents / 100).toFixed(2) }}</div>
              </button>
            </div>
          </div>

          <!-- Subscription tiers as 3 prominent cards -->
          <div class="bp-section">
            <p class="bp-section-label">Or subscribe monthly</p>
            <div class="bp-tier-grid">
              <button
                v-for="(tier, idx) in store.tiers"
                :key="tier.id"
                @click="store.subscribeTier(tier.id)"
                :disabled="store.purchaseLoading !== null || store.subscription.subscribed"
                class="bp-tier"
                :class="{ 'bp-tier-featured': idx === 1 }"
              >
                <span v-if="idx === 1" class="bp-tier-badge">Popular</span>
                <p class="bp-tier-name">{{ tier.id }}</p>
                <p class="bp-tier-tokens">{{ tier.tokens_per_month.toLocaleString() }}</p>
                <p class="bp-tier-tokens-label">tokens / month</p>
                <p class="bp-tier-price">${{ (tier.price_cents / 100).toFixed(0) }} <span class="bp-tier-price-cycle">/ mo</span></p>
              </button>
            </div>
            <p v-if="store.subscription.subscribed" class="bp-tier-foot">
              Already subscribed — cancel current plan to switch.
            </p>
          </div>
        </div>
      </div>
    </transition>
  </teleport>
</template>

<style scoped>
/* ============================================================ *
 * BillingPanel — bible-aligned restructure (rev. 2026-05-02).
 * Hero balance + tile-pattern token packs + featured tier card
 * + AuthLayout-DNA modal chrome.
 * ============================================================ */

/* ── Skeleton ─────────────────────────────────────────────── */
.bp-skeleton-stack { display: flex; flex-direction: column; gap: 1.5rem; }
.bp-skeleton {
  border-radius: 16px;
  background: linear-gradient(110deg, oklch(0.18 0.008 280) 30%, oklch(0.22 0.009 280) 50%, oklch(0.18 0.008 280) 70%);
  background-size: 400% 100%;
  animation: bp-pulse 1.6s ease-in-out infinite;
}
.bp-skeleton-hero { height: 14rem; }
.bp-skeleton-history { height: 16rem; }
@keyframes bp-pulse { 0% { background-position: 100% 0; } 100% { background-position: -100% 0; } }

/* ── Hero ─────────────────────────────────────────────────── */
.bp-hero {
  position: relative;
  padding: 2rem 2rem 1.5rem;
  border-radius: 16px;
  background: oklch(0.18 0.008 280);
  border: 1px solid var(--border-default, #404040);
  box-shadow:
    inset 0 1.5px 0 rgba(255, 255, 255, 0.10),
    inset 0 0 0 1px rgba(255, 255, 255, 0.04),
    0 24px 60px -16px rgba(0, 0, 0, 0.5),
    0 0 80px -20px rgba(163, 230, 53, 0.18);
  margin-bottom: 1.5rem;
  overflow: hidden;
}
.bp-hero-edge {
  position: absolute; top: 0; left: 0; right: 0; height: 1.5px;
  background: linear-gradient(90deg,
    transparent 0%,
    rgba(163, 230, 53, 0.4) 25%,
    rgba(163, 230, 53, 0.9) 50%,
    rgba(163, 230, 53, 0.4) 75%,
    transparent 100%);
  pointer-events: none;
  z-index: 2;
}
.bp-hero-glow {
  position: absolute;
  top: -10%; right: -5%;
  width: 320px; height: 320px;
  background: radial-gradient(circle at center, rgba(163, 230, 53, 0.10) 0%, transparent 65%);
  pointer-events: none;
  z-index: 0;
}
.bp-hero-row {
  position: relative;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1.5rem;
  flex-wrap: wrap;
}
.bp-hero-block { flex: 1; min-width: 0; }
.bp-hero-label {
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--text-tertiary, #737373);
  text-transform: uppercase;
  letter-spacing: 0.12em;
  margin-bottom: 0.5rem;
}
.bp-hero-num {
  font-size: clamp(3rem, 8vw, 5rem);
  font-weight: 700;
  line-height: 1;
  letter-spacing: -0.04em;
  color: var(--accent-brand, #a3e635);
  filter: drop-shadow(0 0 24px rgba(163, 230, 53, 0.30));
  margin-bottom: 0.5rem;
  font-variant-numeric: tabular-nums;
}
.bp-hero-token-label {
  font-size: 0.875rem;
  color: var(--text-tertiary, #737373);
  letter-spacing: 0.04em;
}
.bp-hero-row > .bp-cta-primary { margin-top: 0.25rem; }

/* ── Bar + breakdown ──────────────────────────────────────── */
.bp-bar {
  position: relative;
  display: flex;
  height: 8px;
  border-radius: 999px;
  overflow: hidden;
  background: oklch(0.13 0.005 280);
  margin: 1.75rem 0 1rem;
  box-shadow: inset 0 1px 0 rgba(0, 0, 0, 0.4);
}
.bp-bar-segment {
  height: 100%;
  transition: width 500ms cubic-bezier(0.2, 0, 0, 1);
}
.bp-bar-free      { background: var(--color-lime-300, #bef264); }
.bp-bar-sub       { background: var(--color-lime-500, #84cc16); }
.bp-bar-purchased { background: var(--color-lime-700, #4d7c0f); }

.bp-breakdown {
  position: relative;
  display: flex;
  flex-wrap: wrap;
  gap: 1.25rem;
  font-size: 0.75rem;
  color: var(--text-tertiary, #737373);
}
.bp-breakdown-item {
  display: inline-flex;
  align-items: center;
  gap: 0.4375rem;
}
.bp-breakdown-dot {
  width: 8px; height: 8px;
  border-radius: 50%;
}
.bp-dot-free      { background: var(--color-lime-300, #bef264); }
.bp-dot-sub       { background: var(--color-lime-500, #84cc16); }
.bp-dot-purchased { background: var(--color-lime-700, #4d7c0f); }
.bp-breakdown-num {
  color: var(--text-secondary, #a3a3a3);
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

/* ── Buttons ─────────────────────────────────────────────── */
.bp-cta-primary {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.6875rem 1.25rem;
  border-radius: 8px;
  border: 1px solid transparent;
  background: var(--accent-brand, #a3e635);
  color: var(--accent-fg, #0a0a0a);
  font-weight: 600;
  font-size: 0.9375rem;
  font-family: inherit;
  cursor: pointer;
  transition: all 200ms cubic-bezier(0.2, 0, 0, 1);
  flex-shrink: 0;
}
.bp-cta-primary:hover:not(:disabled) {
  background: var(--accent-brand-strong, #84cc16);
  box-shadow: 0 0 24px rgba(163, 230, 53, 0.45);
  transform: translateY(-1px);
}
.bp-cta-primary:active:not(:disabled) {
  transform: translateY(0);
  box-shadow: 0 0 12px rgba(163, 230, 53, 0.30);
}
.bp-cta-primary:disabled { opacity: 0.5; cursor: not-allowed; }
.bp-cta-primary:focus-visible {
  outline: 2px solid var(--accent-brand, #a3e635);
  outline-offset: 2px;
}

.bp-cta-ghost {
  display: inline-flex;
  align-items: center;
  padding: 0.4375rem 0.875rem;
  border-radius: 8px;
  border: 1px solid var(--border-default, #404040);
  background: transparent;
  color: var(--text-tertiary, #737373);
  font-size: 0.8125rem;
  font-weight: 500;
  font-family: inherit;
  cursor: pointer;
  transition: all 150ms ease;
}
.bp-cta-ghost:hover {
  border-color: var(--feedback-danger, #ef4444);
  color: var(--feedback-danger, #ef4444);
  background: rgba(239, 68, 68, 0.05);
}

/* ── Cards (subscription + history) ──────────────────────── */
.bp-card {
  border-radius: 14px;
  background: oklch(0.18 0.008 280);
  border: 1px solid var(--border-subtle, #262626);
  box-shadow:
    inset 0 1px 0 rgba(255, 255, 255, 0.05),
    0 12px 32px -12px rgba(0, 0, 0, 0.4);
  margin-bottom: 1.5rem;
  overflow: hidden;
}
.bp-subscription { padding: 1.5rem; }

.bp-sub-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1rem;
  flex-wrap: wrap;
}
.bp-sub-block { flex: 1; min-width: 0; }
.bp-sub-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  align-items: center;
}
.bp-card-label {
  font-size: 0.6875rem;
  font-weight: 600;
  color: var(--text-tertiary, #737373);
  text-transform: uppercase;
  letter-spacing: 0.12em;
  margin-bottom: 0.375rem;
}
.bp-sub-tier {
  font-size: 1.375rem;
  font-weight: 700;
  color: var(--text-primary, #e7e5e4);
  letter-spacing: -0.01em;
  text-transform: capitalize;
}
.bp-sub-meta {
  font-size: 0.8125rem;
  color: var(--text-secondary, #a3a3a3);
  margin-top: 0.25rem;
}
.bp-sub-cancelling {
  color: var(--feedback-warn, #ca8a04);
  font-weight: 500;
}

/* ── Desktop dashboard columns ───────────────────────────── */
/* Mobile: single column, source order = controls (side) then history (main).
   Desktop (>=1024): history fills a wide left column, controls/insights sit
   in a narrower right column, so the Spiritus-width (1600px) frame reads as a
   deliberate dashboard rather than a stretched ribbon. */
.bp-cols { display: flex; flex-direction: column; gap: 1.5rem; }
.bp-col-side { display: flex; flex-direction: column; gap: 1.5rem; }
.bp-col-side > .bp-card { margin-bottom: 0; }
.bp-col-main > .bp-card { margin-bottom: 0; }
@media (min-width: 1024px) {
  .bp-cols {
    display: grid;
    grid-template-columns: minmax(0, 1.9fr) minmax(0, 1fr);
    align-items: start;
    gap: 1.75rem;
  }
  .bp-col-main { order: 0; }   /* left, wide */
  .bp-col-side { order: 1; }   /* right, narrow */
}

/* ── Spending cap ────────────────────────────────────────── */
.bp-cap-inner { padding: 1.5rem; }
.bp-cap-title-row { display: flex; align-items: flex-start; gap: 0.625rem; margin-bottom: 1rem; }
.bp-cap-icon { color: var(--accent-brand, #a3e635); flex-shrink: 0; margin-top: 0.0625rem; }
.bp-cap-hint {
  font-size: 0.75rem;
  color: var(--text-tertiary, #737373);
  margin-top: 0.25rem;
  max-width: 30rem;
  line-height: 1.4;
}
.bp-cap-controls { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
.bp-cap-input {
  width: 9rem;
  font-size: 0.9375rem;
  font-family: inherit;
  font-variant-numeric: tabular-nums;
  padding: 0.5rem 0.75rem;
  border-radius: 8px;
  border: 1px solid var(--border-default, #404040);
  background: oklch(0.13 0.005 280);
  color: var(--text-primary, #e7e5e4);
}
.bp-cap-input:focus-visible {
  outline: 2px solid var(--accent-brand, #a3e635);
  outline-offset: 1px;
}
.bp-cap-save {
  padding: 0.5rem 1rem;
  border-radius: 8px;
  border: 0;
  background: var(--accent-brand, #a3e635);
  color: var(--accent-fg, #0a0a0a);
  font-weight: 600;
  font-size: 0.8125rem;
  font-family: inherit;
  cursor: pointer;
  transition: all 150ms ease;
}
.bp-cap-save:hover:not(:disabled) { background: var(--accent-brand-strong, #84cc16); }
.bp-cap-save:disabled { opacity: 0.5; cursor: not-allowed; }
.bp-cap-clear {
  padding: 0.5rem 0.875rem;
  border-radius: 8px;
  border: 1px solid var(--border-default, #404040);
  background: transparent;
  color: var(--text-tertiary, #737373);
  font-size: 0.8125rem;
  font-family: inherit;
  cursor: pointer;
  transition: all 150ms ease;
}
.bp-cap-clear:hover:not(:disabled) { color: var(--text-primary, #e7e5e4); border-color: var(--text-tertiary, #737373); }
.bp-cap-progress { margin-top: 1.125rem; }
.bp-cap-track { height: 8px; border-radius: 999px; background: oklch(0.13 0.005 280); overflow: hidden; }
.bp-cap-fill {
  height: 100%;
  border-radius: 999px;
  background: var(--accent-brand, #a3e635);
  transition: width 400ms cubic-bezier(0.2, 0, 0, 1);
}
.bp-cap-fill-warn { background: var(--feedback-warn, #ca8a04); }
.bp-cap-meta {
  font-size: 0.75rem;
  color: var(--text-secondary, #a3a3a3);
  margin-top: 0.5rem;
  font-variant-numeric: tabular-nums;
}

/* ── Auto top-up ─────────────────────────────────────────── */
.bp-at-card-row {
  display: flex;
  align-items: center;
  gap: 0.625rem;
  padding: 0.75rem 0.875rem;
  border-radius: 9px;
  background: oklch(0.13 0.005 280);
  border: 1px solid var(--border-subtle, #262626);
  color: var(--text-secondary, #a3a3a3);
  margin-bottom: 1rem;
}
.bp-at-card-text { font-size: 0.8125rem; text-transform: capitalize; }
.bp-at-card-text.bp-at-muted { color: var(--text-tertiary, #737373); text-transform: none; }
.bp-at-card-row > button { margin-left: auto; }
.bp-at-cardform {
  padding: 0.875rem;
  border-radius: 9px;
  background: oklch(0.13 0.005 280);
  border: 1px solid var(--border-default, #404040);
  margin-bottom: 1rem;
}
.bp-at-card-element { padding: 0.625rem 0.75rem; border-radius: 8px; background: oklch(0.18 0.008 280); }
.bp-at-cardform-actions { display: flex; gap: 0.5rem; margin-top: 0.75rem; }
.bp-at-error { font-size: 0.75rem; color: var(--feedback-danger, #ef4444); margin-top: 0.5rem; }
.bp-at-settings { display: flex; flex-direction: column; gap: 0.875rem; }
.bp-at-settings.bp-at-disabled { opacity: 0.5; }
.bp-at-toggle { display: inline-flex; align-items: center; gap: 0.5rem; font-size: 0.8125rem; color: var(--text-secondary, #a3a3a3); cursor: pointer; }
.bp-at-fields { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
.bp-at-field { display: inline-flex; align-items: center; gap: 0.5rem; font-size: 0.8125rem; color: var(--text-tertiary, #737373); }
.bp-at-field .bp-cap-input { width: 6rem; }

/* ── Usage breakdown ─────────────────────────────────────── */
.bp-usage { padding: 0; }
.bp-card-title-icon { vertical-align: -2px; margin-right: 0.375rem; color: var(--text-tertiary, #737373); }
.bp-range { display: inline-flex; gap: 0.25rem; }
.bp-range-btn {
  font-size: 0.6875rem;
  font-weight: 600;
  padding: 0.25rem 0.5rem;
  border-radius: 6px;
  border: 1px solid var(--border-subtle, #262626);
  background: transparent;
  color: var(--text-tertiary, #737373);
  cursor: pointer;
  font-family: inherit;
  transition: all 150ms ease;
}
.bp-range-btn:hover { color: var(--text-secondary, #a3a3a3); border-color: var(--border-default, #404040); }
.bp-range-active {
  color: var(--accent-fg, #0a0a0a);
  background: var(--accent-brand, #a3e635);
  border-color: var(--accent-brand, #a3e635);
}
.bp-usage-list { list-style: none; margin: 0; padding: 1rem 1.5rem 1.25rem; display: flex; flex-direction: column; gap: 0.875rem; }
.bp-usage-head { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.375rem; }
.bp-usage-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.bp-usage-label { font-size: 0.8125rem; color: var(--text-secondary, #a3a3a3); }
.bp-usage-num {
  margin-left: auto;
  font-size: 0.8125rem;
  font-weight: 700;
  color: var(--text-primary, #e7e5e4);
  font-variant-numeric: tabular-nums;
}
.bp-usage-track { height: 6px; border-radius: 999px; background: oklch(0.13 0.005 280); overflow: hidden; }
.bp-usage-fill { height: 100%; border-radius: 999px; transition: width 500ms cubic-bezier(0.2, 0, 0, 1); }

/* ── History ─────────────────────────────────────────────── */
.bp-history { padding: 0; }
.bp-history-header { flex-wrap: wrap; gap: 0.625rem; }
.bp-history-controls { display: inline-flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
.bp-filter-select {
  font-size: 0.75rem;
  font-family: inherit;
  padding: 0.3125rem 0.5rem;
  border-radius: 7px;
  border: 1px solid var(--border-subtle, #262626);
  background: oklch(0.21 0.009 280);
  color: var(--text-secondary, #a3a3a3);
  cursor: pointer;
}
.bp-filter-select:hover { border-color: var(--border-default, #404040); }
.bp-export-btn {
  display: inline-flex;
  align-items: center;
  gap: 0.3125rem;
  font-size: 0.75rem;
  font-weight: 600;
  font-family: inherit;
  padding: 0.3125rem 0.625rem;
  border-radius: 7px;
  border: 1px solid var(--border-subtle, #262626);
  background: transparent;
  color: var(--text-tertiary, #737373);
  cursor: pointer;
  transition: all 150ms ease;
}
.bp-export-btn:hover:not(:disabled) {
  border-color: var(--accent-brand, #a3e635);
  color: var(--accent-brand, #a3e635);
  background: rgba(163, 230, 53, 0.06);
}
.bp-export-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.bp-tx-reason-dot {
  display: inline-block;
  width: 6px; height: 6px;
  border-radius: 50%;
  margin-right: 0.375rem;
  vertical-align: 1px;
}
.bp-card-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  padding: 1.25rem 1.5rem;
  border-bottom: 1px solid var(--border-subtle, #262626);
}
.bp-card-title {
  font-size: 0.9375rem;
  font-weight: 600;
  color: var(--text-primary, #e7e5e4);
}
.bp-card-meta {
  font-size: 0.6875rem;
  color: var(--text-tertiary, #737373);
  font-variant-numeric: tabular-nums;
}

.bp-empty {
  padding: 3.5rem 1.5rem;
  text-align: center;
  color: var(--text-tertiary, #737373);
}
.bp-empty svg { opacity: 0.4; margin-bottom: 0.5rem; }
.bp-empty p { font-size: 0.875rem; margin-top: 0.25rem; }
.bp-empty-hint { font-size: 0.75rem; opacity: 0.7; }

.bp-tx-list { list-style: none; padding: 0; margin: 0; }
.bp-tx-row {
  display: flex;
  align-items: center;
  gap: 0.875rem;
  padding: 0.875rem 1.5rem;
  border-bottom: 1px solid var(--border-subtle, #262626);
  transition: background 150ms ease;
}
.bp-tx-row:last-child { border-bottom: 0; }
.bp-tx-row:hover { background: oklch(0.21 0.009 280 / 0.5); }
.bp-tx-icon {
  flex-shrink: 0;
  width: 2rem;
  height: 2rem;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.bp-tx-icon-pos {
  background: rgba(163, 230, 53, 0.12);
  color: var(--accent-brand, #a3e635);
}
.bp-tx-icon-neg {
  background: rgba(239, 68, 68, 0.10);
  color: var(--feedback-danger, #ef4444);
}
.bp-tx-body { flex: 1; min-width: 0; }
.bp-tx-label {
  font-size: 0.875rem;
  color: var(--text-primary, #e7e5e4);
  font-weight: 500;
}
.bp-tx-reason {
  font-size: 0.75rem;
  color: var(--text-tertiary, #737373);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  margin-top: 0.0625rem;
}
.bp-tx-amount-block {
  flex-shrink: 0;
  text-align: right;
}
.bp-tx-amount {
  font-size: 0.875rem;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.01em;
}
.bp-tx-amount-pos { color: var(--accent-brand, #a3e635); }
.bp-tx-amount-neg { color: var(--feedback-danger, #ef4444); }
.bp-tx-date {
  font-size: 0.6875rem;
  color: var(--text-tertiary, #737373);
  margin-top: 0.0625rem;
  font-variant-numeric: tabular-nums;
}

/* ════════════════════════════════════════════════════════════
 * MODAL — AuthLayout DNA (edge line + corners + glow)
 * ════════════════════════════════════════════════════════════ */

.bp-modal-backdrop {
  position: fixed;
  inset: 0;
  z-index: 100;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 1rem;
  background: rgba(0, 0, 0, 0.65);
  backdrop-filter: blur(6px);
  -webkit-backdrop-filter: blur(6px);
}
.bp-modal {
  position: relative;
  width: 100%;
  max-width: 28rem;
  max-height: calc(100vh - 2rem);
  overflow: auto;
  padding: 1.75rem;
  border-radius: 16px;
  background: oklch(0.18 0.008 280 / 0.95);
  border: 1px solid var(--border-default, #404040);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  box-shadow:
    inset 0 1.5px 0 rgba(255, 255, 255, 0.12),
    inset 0 0 0 1px rgba(255, 255, 255, 0.05),
    inset 0 -2px 0 rgba(0, 0, 0, 0.30),
    0 32px 72px -16px rgba(0, 0, 0, 0.7),
    0 0 100px -20px rgba(163, 230, 53, 0.20);
}
.bp-modal-edge {
  position: absolute; top: 0; left: 0; right: 0; height: 1.5px;
  background: linear-gradient(90deg,
    transparent 0%,
    rgba(163, 230, 53, 0.4) 25%,
    rgba(163, 230, 53, 0.9) 50%,
    rgba(163, 230, 53, 0.4) 75%,
    transparent 100%);
  pointer-events: none;
  z-index: 2;
}
.bp-modal-corner {
  position: absolute;
  width: 14px; height: 14px;
  border: 1.5px solid rgba(163, 230, 53, 0.6);
  pointer-events: none;
  z-index: 3;
}
.bp-modal-corner.tl { top: 8px; left: 8px; border-right: 0; border-bottom: 0; border-radius: 4px 0 0 0; }
.bp-modal-corner.tr { top: 8px; right: 8px; border-left: 0; border-bottom: 0; border-radius: 0 4px 0 0; }
.bp-modal-corner.bl { bottom: 8px; left: 8px; border-right: 0; border-top: 0; border-radius: 0 0 0 4px; }
.bp-modal-corner.br { bottom: 8px; right: 8px; border-left: 0; border-top: 0; border-radius: 0 0 4px 0; }

.bp-modal-close {
  position: absolute;
  top: 12px; right: 12px;
  width: 30px; height: 30px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--text-tertiary, #737373);
  cursor: pointer;
  transition: all 150ms ease;
  z-index: 4;
}
.bp-modal-close:hover {
  background: oklch(0.22 0.009 280);
  color: var(--text-primary, #e7e5e4);
}

.bp-modal-header {
  display: flex;
  align-items: center;
  gap: 0.875rem;
  margin-bottom: 1.5rem;
}
.bp-modal-icon {
  width: 2.5rem;
  height: 2.5rem;
  border-radius: 10px;
  background: rgba(163, 230, 53, 0.12);
  border: 1px solid rgba(163, 230, 53, 0.30);
  color: var(--accent-brand, #a3e635);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  box-shadow: 0 0 16px rgba(163, 230, 53, 0.20);
}
.bp-modal-title {
  font-size: 1.125rem;
  font-weight: 700;
  color: var(--text-primary, #e7e5e4);
  letter-spacing: -0.01em;
}
.bp-modal-sub {
  font-size: 0.8125rem;
  color: var(--text-tertiary, #737373);
  margin-top: 0.125rem;
}

.bp-section { margin-bottom: 1.25rem; }
.bp-section:last-child { margin-bottom: 0; }
.bp-section-label {
  font-size: 0.625rem;
  font-weight: 700;
  color: var(--text-tertiary, #737373);
  text-transform: uppercase;
  letter-spacing: 0.12em;
  margin-bottom: 0.625rem;
}

/* ── Token packs — pressable rows with depth ─────────────── */
.bp-pack-list { display: flex; flex-direction: column; gap: 0.5rem; }
.bp-pack {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  padding: 0.875rem 1rem;
  border-radius: 10px;
  border: 1px solid var(--border-subtle, #262626);
  background: oklch(0.21 0.009 280);
  color: inherit;
  font-family: inherit;
  cursor: pointer;
  transition: all 200ms cubic-bezier(0.34, 1.56, 0.64, 1);
  box-shadow:
    inset 0 1px 0 rgba(255, 255, 255, 0.04),
    inset 0 -1px 0 rgba(0, 0, 0, 0.20),
    0 4px 10px -3px rgba(0, 0, 0, 0.30);
}
.bp-pack:hover:not(:disabled) {
  border-color: var(--accent-brand, #a3e635);
  background: oklch(0.24 0.010 280);
  transform: translateY(-2px);
  box-shadow:
    inset 0 1.5px 0 rgba(255, 255, 255, 0.10),
    inset 0 -1px 0 rgba(0, 0, 0, 0.25),
    0 12px 24px -8px rgba(0, 0, 0, 0.50);
}
.bp-pack:active:not(:disabled) {
  transform: translateY(0);
  box-shadow:
    inset 0 2px 6px rgba(0, 0, 0, 0.30),
    inset 0 -1px 0 rgba(255, 255, 255, 0.04);
}
.bp-pack:disabled { opacity: 0.4; cursor: not-allowed; }

.bp-pack-featured {
  border-color: rgba(163, 230, 53, 0.50);
  background: linear-gradient(135deg, oklch(0.22 0.009 280), oklch(0.24 0.012 280));
  box-shadow:
    inset 0 1.5px 0 rgba(255, 255, 255, 0.08),
    inset 0 -1px 0 rgba(0, 0, 0, 0.22),
    0 6px 14px -3px rgba(0, 0, 0, 0.35),
    0 0 28px -10px rgba(163, 230, 53, 0.40);
}
.bp-pack-badge {
  position: absolute;
  top: -8px;
  left: 12px;
  font-size: 0.5625rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  padding: 0.1875rem 0.5rem;
  border-radius: 999px;
  background: var(--accent-brand, #a3e635);
  color: var(--accent-fg, #0a0a0a);
  box-shadow: 0 4px 12px -2px rgba(163, 230, 53, 0.45);
}

.bp-pack-tokens { display: flex; align-items: baseline; gap: 0.4375rem; }
.bp-pack-tokens-num {
  font-size: 1.125rem;
  font-weight: 700;
  color: var(--accent-brand, #a3e635);
  font-variant-numeric: tabular-nums;
}
.bp-pack-tokens-label {
  font-size: 0.75rem;
  color: var(--text-tertiary, #737373);
  letter-spacing: 0.02em;
}
.bp-pack-price {
  font-size: 0.9375rem;
  font-weight: 700;
  color: var(--text-primary, #e7e5e4);
  font-variant-numeric: tabular-nums;
}

/* ── Subscription tiers — 3-col with featured center ───── */
.bp-tier-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 0.5rem;
}
.bp-tier {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  padding: 1rem 0.625rem;
  border-radius: 10px;
  border: 1px solid var(--border-subtle, #262626);
  background: oklch(0.21 0.009 280);
  color: inherit;
  font-family: inherit;
  cursor: pointer;
  transition: all 200ms cubic-bezier(0.34, 1.56, 0.64, 1);
  box-shadow:
    inset 0 1px 0 rgba(255, 255, 255, 0.04),
    inset 0 -1px 0 rgba(0, 0, 0, 0.20),
    0 4px 10px -3px rgba(0, 0, 0, 0.30);
}
.bp-tier:hover:not(:disabled) {
  border-color: var(--accent-brand, #a3e635);
  background: oklch(0.24 0.010 280);
  transform: translateY(-2px);
  box-shadow:
    inset 0 1.5px 0 rgba(255, 255, 255, 0.10),
    inset 0 -1px 0 rgba(0, 0, 0, 0.25),
    0 12px 24px -8px rgba(0, 0, 0, 0.50);
}
.bp-tier:active:not(:disabled) { transform: translateY(0); }
.bp-tier:disabled { opacity: 0.4; cursor: not-allowed; }

.bp-tier-featured {
  border-color: rgba(163, 230, 53, 0.50);
  background: linear-gradient(180deg, oklch(0.24 0.010 280), oklch(0.22 0.009 280));
  box-shadow:
    inset 0 1.5px 0 rgba(255, 255, 255, 0.10),
    inset 0 -1px 0 rgba(0, 0, 0, 0.22),
    0 6px 14px -3px rgba(0, 0, 0, 0.35),
    0 0 28px -10px rgba(163, 230, 53, 0.40);
  transform: translateY(-4px);
}
.bp-tier-featured:hover:not(:disabled) { transform: translateY(-6px); }
.bp-tier-badge {
  position: absolute;
  top: -8px;
  left: 50%;
  transform: translateX(-50%);
  font-size: 0.5625rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  padding: 0.1875rem 0.5rem;
  border-radius: 999px;
  background: var(--accent-brand, #a3e635);
  color: var(--accent-fg, #0a0a0a);
  box-shadow: 0 4px 12px -2px rgba(163, 230, 53, 0.45);
  white-space: nowrap;
}

.bp-tier-name {
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--text-secondary, #a3a3a3);
  text-transform: capitalize;
  letter-spacing: 0.04em;
  margin-bottom: 0.5rem;
}
.bp-tier-tokens {
  font-size: 1.375rem;
  font-weight: 700;
  color: var(--accent-brand, #a3e635);
  font-variant-numeric: tabular-nums;
  line-height: 1;
}
.bp-tier-tokens-label {
  font-size: 0.625rem;
  color: var(--text-tertiary, #737373);
  margin-top: 0.1875rem;
  letter-spacing: 0.04em;
}
.bp-tier-price {
  font-size: 0.875rem;
  font-weight: 700;
  color: var(--text-primary, #e7e5e4);
  margin-top: 0.625rem;
  font-variant-numeric: tabular-nums;
}
.bp-tier-price-cycle {
  font-size: 0.6875rem;
  font-weight: 500;
  color: var(--text-tertiary, #737373);
}
.bp-tier-foot {
  font-size: 0.6875rem;
  color: var(--text-tertiary, #737373);
  text-align: center;
  margin-top: 0.75rem;
  font-style: italic;
}

/* ── Modal transitions ───────────────────────────────────── */
.bp-modal-enter-active { transition: opacity 200ms ease; }
.bp-modal-enter-active .bp-modal { transition: transform 200ms cubic-bezier(0.34, 1.56, 0.64, 1), opacity 200ms ease; }
.bp-modal-leave-active { transition: opacity 150ms ease; }
.bp-modal-leave-active .bp-modal { transition: transform 150ms ease, opacity 150ms ease; }
.bp-modal-enter-from,
.bp-modal-leave-to { opacity: 0; }
.bp-modal-enter-from .bp-modal,
.bp-modal-leave-to .bp-modal { transform: scale(0.96); opacity: 0; }

@media (prefers-reduced-motion: reduce) {
  .bp-skeleton,
  .bp-cta-primary,
  .bp-cta-ghost,
  .bp-pack,
  .bp-tier,
  .bp-tx-row,
  .bp-bar-segment,
  .bp-modal-enter-active,
  .bp-modal-leave-active { transition: none; animation: none; }
  .bp-cta-primary:hover,
  .bp-pack:hover,
  .bp-tier:hover,
  .bp-tier-featured,
  .bp-tier-featured:hover { transform: none; }
}
</style>
