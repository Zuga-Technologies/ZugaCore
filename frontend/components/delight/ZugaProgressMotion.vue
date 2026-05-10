<script setup lang="ts">
import { computed, watch, ref } from 'vue'

const props = withDefaults(defineProps<{
  value: number
  max?: number
  variant?: 'ring' | 'bar'
  size?: number
  label?: string
  celebrateOnFull?: boolean
}>(), {
  max: 100,
  variant: 'ring',
  size: 96,
  label: '',
  celebrateOnFull: true,
})

const emit = defineEmits<{ (e: 'milestone'): void }>()

const pct = computed(() => Math.max(0, Math.min(1, props.value / props.max)))
const celebrating = ref(false)

watch(pct, (n, o) => {
  if (props.celebrateOnFull && n >= 1 && o < 1) {
    celebrating.value = true
    emit('milestone')
    window.setTimeout(() => { celebrating.value = false }, 900)
  }
})

// ring math
const r = computed(() => (props.size - 12) / 2)
const c = computed(() => 2 * Math.PI * r.value)
const dash = computed(() => `${pct.value * c.value} ${c.value}`)
</script>

<template>
  <div
    class="zd-progress"
    :class="[`is-${variant}`, { 'is-celebrating': celebrating }]"
    :aria-label="label || `progress ${Math.round(pct * 100)} percent`"
    role="progressbar"
    :aria-valuenow="value"
    :aria-valuemin="0"
    :aria-valuemax="max"
  >
    <svg
      v-if="variant === 'ring'"
      :width="size"
      :height="size"
      :viewBox="`0 0 ${size} ${size}`"
    >
      <circle
        :cx="size/2" :cy="size/2" :r="r"
        fill="none"
        stroke="rgba(var(--accent), 0.18)"
        stroke-width="6"
      />
      <circle
        class="zd-progress-arc"
        :cx="size/2" :cy="size/2" :r="r"
        fill="none"
        stroke="rgb(var(--accent))"
        stroke-width="6"
        stroke-linecap="round"
        :stroke-dasharray="dash"
        :transform="`rotate(-90 ${size/2} ${size/2})`"
      />
    </svg>

    <div v-else class="zd-progress-bar">
      <div
        class="zd-progress-bar-fill"
        :style="{ width: `${pct * 100}%` }"
      />
    </div>

    <div class="zd-progress-text">
      <slot>{{ Math.round(pct * 100) }}%</slot>
    </div>
  </div>
</template>

<style scoped>
.zd-progress {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.zd-progress.is-bar {
  display: block;
  width: 100%;
}
.zd-progress-arc {
  transition: stroke-dasharray var(--zd-dur-feature) var(--zd-ease-soft);
  filter: drop-shadow(0 0 0 transparent);
}
.is-celebrating .zd-progress-arc {
  filter: drop-shadow(0 0 8px var(--zd-glow-celebrate));
  animation: zd-progress-pulse var(--zd-dur-arrival) var(--zd-ease-celebrate);
}
.zd-progress-text {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-variant-numeric: tabular-nums;
  color: rgb(var(--txt-primary));
}
.is-bar .zd-progress-text {
  position: static;
  margin-top: 6px;
  font-size: 12px;
  color: rgb(var(--txt-secondary));
}
.zd-progress-bar {
  width: 100%;
  height: 8px;
  border-radius: 999px;
  background: rgba(var(--accent), 0.15);
  overflow: hidden;
}
.zd-progress-bar-fill {
  height: 100%;
  background: linear-gradient(
    90deg,
    rgb(var(--accent)) 0%,
    rgb(var(--accent-bright, var(--accent))) 100%
  );
  border-radius: 999px;
  transition: width var(--zd-dur-feature) var(--zd-ease-soft);
}
.is-celebrating .zd-progress-bar-fill {
  box-shadow: 0 0 14px var(--zd-glow-celebrate);
}

@keyframes zd-progress-pulse {
  0%   { transform: scale(1);    }
  40%  { transform: scale(1.04); }
  100% { transform: scale(1);    }
}
</style>
