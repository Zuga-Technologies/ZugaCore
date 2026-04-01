<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useTokenStore } from './useTokens'
import { txTypeLabel, formatDate, formatReason } from './helpers'
import {
  Coins, ShoppingCart, CreditCard, TrendingDown, TrendingUp,
  RefreshCw, Gift, Zap, Clock, Infinity,
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
  free_refill: RefreshCw,
  subscription: CreditCard,
  grant: Gift,
  expire: Clock,
  refund: TrendingUp,
}

function openPurchase() {
  store.loadPacks()
  showPurchaseModal.value = true
}

onMounted(() => store.fetchAll())
</script>

<template>
  <!-- Loading skeleton -->
  <template v-if="store.loading">
    <div class="space-y-6 animate-pulse">
      <div class="h-40 bg-surface-1 border border-bdr rounded-xl" />
      <div class="h-64 bg-surface-1 border border-bdr rounded-xl" />
    </div>
  </template>

  <template v-else-if="store.balance">

    <!-- Balance Card -->
    <div class="bg-surface-1 border border-bdr rounded-xl p-6 mb-6">
      <div class="flex items-center justify-between mb-4">
        <div>
          <p class="text-sm text-txt-muted">Available Balance</p>
          <p class="text-4xl font-bold text-accent mt-1">
            <template v-if="store.balance.is_unlimited">
              <Infinity :size="36" class="inline" />
            </template>
            <template v-else>
              {{ Math.round(store.balance.total).toLocaleString() }}
            </template>
          </p>
        </div>
        <button
          v-if="!store.balance.is_unlimited"
          @click="openPurchase"
          class="flex items-center gap-2 px-4 py-2.5 rounded-lg bg-accent text-black font-semibold text-sm hover:bg-accent/90 transition-colors"
        >
          <Zap :size="16" />
          Buy Tokens
        </button>
      </div>

      <!-- Balance breakdown bar -->
      <template v-if="!store.balance.is_unlimited && store.balance.total > 0">
        <div class="flex rounded-full h-3 overflow-hidden bg-surface-2 mb-3">
          <div
            class="bg-emerald-500 transition-all duration-500"
            :style="{ width: store.balancePercent.free + '%' }"
            :title="`Free: ${Math.round(store.balance.free)}`"
          />
          <div
            class="bg-blue-500 transition-all duration-500"
            :style="{ width: store.balancePercent.sub + '%' }"
            :title="`Subscription: ${Math.round(store.balance.subscription)}`"
          />
          <div
            class="bg-accent transition-all duration-500"
            :style="{ width: store.balancePercent.purchased + '%' }"
            :title="`Purchased: ${Math.round(store.balance.purchased)}`"
          />
        </div>
        <div class="flex items-center gap-4 text-xs text-txt-muted">
          <span class="flex items-center gap-1.5">
            <span class="w-2.5 h-2.5 rounded-full bg-emerald-500" />
            Free: {{ Math.round(store.balance.free) }}
          </span>
          <span class="flex items-center gap-1.5">
            <span class="w-2.5 h-2.5 rounded-full bg-blue-500" />
            Subscription: {{ Math.round(store.balance.subscription) }}
          </span>
          <span class="flex items-center gap-1.5">
            <span class="w-2.5 h-2.5 rounded-full bg-accent" />
            Purchased: {{ Math.round(store.balance.purchased) }}
          </span>
        </div>
      </template>
    </div>

    <!-- Subscription Card -->
    <div v-if="store.subscription.subscribed" class="bg-surface-1 border border-bdr rounded-xl p-6 mb-6">
      <div class="flex items-center justify-between">
        <div>
          <p class="text-sm text-txt-muted">Current Plan</p>
          <p class="text-lg font-semibold text-txt-primary mt-0.5 capitalize">
            {{ store.subscription.tier }} Plan
          </p>
          <p class="text-xs text-txt-muted mt-1">
            {{ store.subscription.tokens_per_cycle?.toLocaleString() }} tokens/month
            <template v-if="store.subscription.status === 'cancelled' || store.subscription.status === 'cancelling'">
              &middot; <span class="text-amber-400">Cancels {{ store.subscription.current_period_end ? new Date(store.subscription.current_period_end).toLocaleDateString() : 'soon' }}</span>
            </template>
          </p>
        </div>
        <button
          v-if="store.subscription.status === 'active'"
          @click="store.cancelSubscription()"
          class="px-3 py-1.5 rounded-lg text-xs text-txt-muted border border-bdr hover:border-red-500/50 hover:text-red-400 transition-colors"
        >
          Cancel Plan
        </button>
      </div>
    </div>

    <!-- Transaction History -->
    <div v-if="showHistory" class="bg-surface-1 border border-bdr rounded-xl overflow-hidden">
      <div class="px-6 py-4 border-b border-bdr">
        <h2 class="text-sm font-semibold text-txt-primary">Recent Activity</h2>
      </div>

      <div v-if="store.transactions.length === 0" class="px-6 py-12 text-center">
        <Coins :size="32" class="text-txt-muted mx-auto mb-2 opacity-30" />
        <p class="text-sm text-txt-muted">No token activity yet.</p>
      </div>

      <ul v-else class="divide-y divide-bdr">
        <li
          v-for="tx in store.transactions"
          :key="tx.id"
          class="flex items-center gap-3 px-6 py-3 hover:bg-surface-2/30 transition-colors"
        >
          <div
            class="w-8 h-8 rounded-lg flex items-center justify-center flex-shrink-0"
            :class="tx.amount > 0 ? 'bg-emerald-500/10' : 'bg-red-500/10'"
          >
            <component
              :is="txTypeIcon[tx.type] || Coins"
              :size="15"
              :class="tx.amount > 0 ? 'text-emerald-400' : 'text-red-400'"
            />
          </div>
          <div class="flex-1 min-w-0">
            <p class="text-sm text-txt-primary">{{ txTypeLabel[tx.type] || tx.type }}</p>
            <p v-if="tx.reason" class="text-xs text-txt-muted truncate">{{ formatReason(tx.reason) }}</p>
          </div>
          <div class="text-right flex-shrink-0">
            <p
              class="text-sm font-semibold"
              :class="tx.amount > 0 ? 'text-emerald-400' : 'text-red-400'"
            >
              {{ tx.amount > 0 ? '+' : '' }}{{ Math.round(tx.amount).toLocaleString() }}
            </p>
            <p class="text-[10px] text-txt-muted">{{ formatDate(tx.created_at) }}</p>
          </div>
        </li>
      </ul>
    </div>

  </template>

  <!-- ═══════════════════════════════════════════════════
       PURCHASE MODAL
  ════════════════════════════════════════════════════ -->
  <teleport to="body">
    <transition name="billing-modal">
      <div
        v-if="showPurchaseModal"
        class="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 backdrop-blur-sm"
        @click.self="showPurchaseModal = false"
      >
        <div class="w-full max-w-md mx-4 bg-surface-1 border border-bdr rounded-2xl shadow-2xl shadow-black/30 overflow-hidden animate-fade-in">

          <!-- Header -->
          <div class="px-6 pt-6 pb-4">
            <h2 class="text-lg font-bold text-txt-primary flex items-center gap-2">
              <Coins :size="20" class="text-accent" />
              Buy {{ tokenLabel }}
            </h2>
            <p class="text-xs text-txt-muted mt-1">One-time packs — tokens never expire.</p>
          </div>

          <!-- Top-up packs -->
          <div class="px-6 pb-4 space-y-2">
            <button
              v-for="pack in store.packs"
              :key="pack.id"
              @click="store.buyPack(pack.id)"
              :disabled="store.purchaseLoading !== null"
              class="w-full flex items-center justify-between px-4 py-3 rounded-xl border border-bdr hover:border-accent/50 hover:bg-accent/5 transition-all group"
              :class="{ 'ring-2 ring-accent': pack.id === 'best_value' }"
            >
              <div class="flex items-center gap-3">
                <span class="text-lg font-bold text-accent">{{ pack.tokens.toLocaleString() }}</span>
                <span class="text-xs text-txt-muted">tokens</span>
              </div>
              <div class="flex items-center gap-2">
                <span
                  v-if="pack.id === 'best_value'"
                  class="text-[10px] font-semibold uppercase tracking-wider text-accent bg-accent/10 px-2 py-0.5 rounded-full"
                >Best Value</span>
                <span class="text-sm font-semibold text-txt-primary group-hover:text-accent transition-colors">
                  ${{ (pack.price_cents / 100).toFixed(2) }}
                </span>
              </div>
            </button>
          </div>

          <!-- Subscription tiers -->
          <div class="px-6 pb-6">
            <p class="text-xs text-txt-muted mb-3 uppercase tracking-wider font-semibold">Or subscribe monthly</p>
            <div class="grid grid-cols-3 gap-2">
              <button
                v-for="tier in store.tiers"
                :key="tier.id"
                @click="store.subscribeTier(tier.id)"
                :disabled="store.purchaseLoading !== null || store.subscription.subscribed"
                class="flex flex-col items-center px-3 py-3 rounded-xl border border-bdr hover:border-accent/50 hover:bg-accent/5 transition-all disabled:opacity-40 disabled:cursor-not-allowed"
              >
                <span class="text-xs font-semibold text-txt-primary capitalize">{{ tier.id }}</span>
                <span class="text-lg font-bold text-accent mt-1">{{ tier.tokens_per_month.toLocaleString() }}</span>
                <span class="text-[10px] text-txt-muted">tokens/mo</span>
                <span class="text-xs font-semibold text-txt-secondary mt-1">${{ (tier.price_cents / 100).toFixed(0) }}/mo</span>
              </button>
            </div>
            <p v-if="store.subscription.subscribed" class="text-[10px] text-txt-muted mt-2 text-center">
              Already subscribed — cancel first to switch plans.
            </p>
          </div>

          <!-- Close -->
          <div class="px-6 pb-6">
            <button
              @click="showPurchaseModal = false"
              class="w-full py-2.5 rounded-lg text-sm text-txt-muted hover:text-txt-primary border border-bdr hover:bg-surface-2 transition-colors"
            >
              Close
            </button>
          </div>
        </div>
      </div>
    </transition>
  </teleport>
</template>

<style scoped>
.billing-modal-enter-active { transition: opacity 0.2s ease; }
.billing-modal-leave-active { transition: opacity 0.15s ease; }
.billing-modal-enter-from, .billing-modal-leave-to { opacity: 0; }
</style>
