<script setup lang="ts">
import { Coins, Zap, Clock } from 'lucide-vue-next'

interface Props {
  estimatedCost?: number
  currentBalance?: number
  visible: boolean
}

const props = withDefaults(defineProps<Props>(), {
  estimatedCost: 0,
  currentBalance: 0,
})

const emit = defineEmits<{
  close: []
  buyTokens: []
}>()
</script>

<template>
  <teleport to="body">
    <transition name="insufficient-modal">
      <div
        v-if="visible"
        class="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 backdrop-blur-sm"
        @click.self="emit('close')"
      >
        <div class="w-full max-w-sm mx-4 bg-surface-1 border border-bdr rounded-2xl shadow-2xl shadow-black/30 overflow-hidden animate-fade-in">
          <div class="px-6 pt-6 pb-2 text-center">
            <div class="w-14 h-14 rounded-full bg-amber-500/10 flex items-center justify-center mx-auto mb-4">
              <Coins :size="28" class="text-amber-400" />
            </div>
            <h2 class="text-lg font-bold text-txt-primary">Not Enough ZugaTokens</h2>
            <p class="text-sm text-txt-muted mt-2">
              This feature needs
              <span class="font-semibold text-accent">~{{ Math.round(estimatedCost) }}</span>
              tokens.
              You have
              <span class="font-semibold text-txt-primary">{{ Math.round(currentBalance) }}</span>.
            </p>
          </div>

          <div class="px-6 py-5 space-y-2.5">
            <button
              @click="emit('buyTokens')"
              class="w-full flex items-center justify-center gap-2 px-4 py-3 rounded-xl bg-accent text-black font-semibold text-sm hover:bg-accent/90 transition-colors"
            >
              <Zap :size="16" />
              Buy ZugaTokens
            </button>

            <button
              @click="emit('close')"
              class="w-full flex items-center justify-center gap-2 px-4 py-3 rounded-xl border border-bdr text-sm text-txt-muted hover:text-txt-primary hover:bg-surface-2 transition-colors"
            >
              <Clock :size="14" />
              Wait for Tomorrow's Refill
            </button>
          </div>
        </div>
      </div>
    </transition>
  </teleport>
</template>

<style scoped>
.insufficient-modal-enter-active { transition: opacity 0.2s ease; }
.insufficient-modal-leave-active { transition: opacity 0.15s ease; }
.insufficient-modal-enter-from, .insufficient-modal-leave-to { opacity: 0; }
</style>
