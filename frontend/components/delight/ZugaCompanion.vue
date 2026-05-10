<script setup lang="ts">
import { computed } from 'vue'

type Expression = 'idle' | 'happy' | 'thinking' | 'concerned' | 'celebrate'

const props = withDefaults(defineProps<{
  expression?: Expression
  size?: number
  label?: string
}>(), {
  expression: 'idle',
  size: 64,
  label: '',
})

const stateClass = computed(() => `is-${props.expression}`)
</script>

<template>
  <div
    class="zd-companion"
    :class="stateClass"
    :style="{ width: `${size}px`, height: `${size}px` }"
    :aria-label="label || `companion ${expression}`"
    role="img"
  >
    <svg viewBox="0 0 64 64" class="zd-companion-svg">
      <!-- glow halo -->
      <circle cx="32" cy="32" r="30" class="zd-halo" />
      <!-- body -->
      <circle cx="32" cy="32" r="24" class="zd-body" />
      <!-- eyes -->
      <g class="zd-eyes">
        <circle cx="24" cy="30" r="3" class="zd-eye" />
        <circle cx="40" cy="30" r="3" class="zd-eye" />
      </g>
      <!-- mouth -->
      <path d="M24 40 Q32 46 40 40" class="zd-mouth" fill="none" />
    </svg>
  </div>
</template>

<style scoped>
.zd-companion {
  position: relative;
  display: inline-block;
}
.zd-companion-svg {
  width: 100%;
  height: 100%;
  overflow: visible;
}

/* idle breathing */
.zd-body {
  fill: rgb(var(--accent));
  transform-origin: 32px 32px;
  animation: zd-breathe 3.6s var(--zd-ease-soft) infinite;
}
.zd-halo {
  fill: var(--zd-glow-soft);
  transform-origin: 32px 32px;
  animation: zd-halo-pulse 4s var(--zd-ease-soft) infinite;
}
.zd-eye  { fill: rgb(var(--surface-0)); }
.zd-mouth {
  stroke: rgb(var(--surface-0));
  stroke-width: 2.4;
  stroke-linecap: round;
  transition: d var(--zd-dur-base) var(--zd-ease-soft);
}

/* expressions */
.is-happy .zd-mouth     { d: path("M22 38 Q32 50 42 38"); }
.is-thinking .zd-mouth  { d: path("M26 42 L38 42"); }
.is-thinking .zd-body   { animation: zd-tilt 2.4s var(--zd-ease-soft) infinite; }
.is-concerned .zd-mouth { d: path("M24 44 Q32 38 40 44"); }
.is-concerned .zd-body  { fill: rgb(var(--accent-dim, var(--accent))); }
.is-celebrate .zd-body  { animation: zd-bounce var(--zd-dur-arrival) var(--zd-ease-celebrate) infinite; }
.is-celebrate .zd-halo  { fill: var(--zd-glow-celebrate); animation-duration: 1.6s; }
.is-celebrate .zd-mouth { d: path("M22 38 Q32 52 42 38"); }

@keyframes zd-breathe {
  0%, 100% { transform: scale(1); }
  50%      { transform: scale(1.04); }
}
@keyframes zd-halo-pulse {
  0%, 100% { opacity: 0.5; transform: scale(0.96); }
  50%      { opacity: 0.9; transform: scale(1.06); }
}
@keyframes zd-tilt {
  0%, 100% { transform: rotate(-3deg); }
  50%      { transform: rotate(3deg); }
}
@keyframes zd-bounce {
  0%, 100% { transform: translateY(0) scale(1); }
  35%      { transform: translateY(-8%) scale(1.08); }
  70%      { transform: translateY(0) scale(0.96); }
}
</style>
