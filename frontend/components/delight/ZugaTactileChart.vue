<script setup lang="ts">
import { computed, ref } from 'vue'

const props = withDefaults(defineProps<{
  values: number[]
  labels?: string[]
  height?: number
  ariaLabel?: string
}>(), {
  height: 120,
  labels: () => [],
  ariaLabel: 'Data series',
})

const emit = defineEmits<{
  (e: 'hover', payload: { index: number; value: number }): void
  (e: 'leave'): void
}>()

const hoverIdx = ref<number | null>(null)

const max = computed(() => Math.max(1, ...props.values))
const min = computed(() => Math.min(0, ...props.values))
const range = computed(() => Math.max(1, max.value - min.value))

const onEnter = (i: number) => {
  hoverIdx.value = i
  emit('hover', { index: i, value: props.values[i] })
}
const onLeave = () => {
  hoverIdx.value = null
  emit('leave')
}
</script>

<template>
  <div
    class="zd-chart"
    :style="{ height: `${height}px` }"
    role="img"
    :aria-label="ariaLabel"
    @pointerleave="onLeave"
  >
    <div
      v-for="(v, i) in values"
      :key="i"
      class="zd-chart-bar"
      :class="{ 'is-hover': hoverIdx === i }"
      :style="{
        height: `${((v - min) / range) * 100}%`,
        animationDelay: `${i * 24}ms`,
      }"
      @pointerenter="onEnter(i)"
      @focus="onEnter(i)"
      @blur="onLeave"
      tabindex="0"
      :aria-label="`${labels[i] ?? `point ${i+1}`}: ${v}`"
    >
      <span class="zd-chart-fill" />
      <span v-if="hoverIdx === i" class="zd-chart-tip">{{ v }}</span>
    </div>
  </div>
</template>

<style scoped>
.zd-chart {
  display: flex;
  align-items: flex-end;
  gap: 4px;
  width: 100%;
  position: relative;
}
.zd-chart-bar {
  flex: 1 1 0;
  position: relative;
  min-height: 2px;
  border-radius: 4px 4px 2px 2px;
  /* --accent is a space-separated RGB triplet, so the rest state needs the
     modern rgb(... / a) form. The old rgba(var(--accent), 0.18) was invalid
     with a triplet, dropping the background — bars were invisible until hover
     (where rgb(var(--accent)) is valid) lit them up. */
  background: rgb(var(--accent) / 0.18);
  outline: none;
  cursor: pointer;
  transition:
    background var(--zd-dur-quick) var(--zd-ease-soft),
    box-shadow var(--zd-dur-quick) var(--zd-ease-soft),
    transform  var(--zd-dur-quick) var(--zd-ease-soft);
  animation: zd-chart-rise var(--zd-dur-feature) var(--zd-ease-soft) backwards;
}
.zd-chart-bar:hover,
.zd-chart-bar:focus-visible,
.zd-chart-bar.is-hover {
  background: rgb(var(--accent));
  box-shadow: 0 0 0 2px var(--zd-glow-soft), 0 0 18px var(--zd-glow-celebrate);
  transform: translateY(-2px);
}
.zd-chart-fill {
  display: none;
}
.zd-chart-tip {
  position: absolute;
  bottom: calc(100% + 6px);
  left: 50%;
  transform: translateX(-50%);
  padding: 2px 8px;
  border-radius: 6px;
  font-size: 11px;
  line-height: 1.4;
  color: rgb(var(--surface-0));
  background: rgb(var(--accent));
  white-space: nowrap;
  pointer-events: none;
  animation: zd-chart-tip-in var(--zd-dur-quick) var(--zd-ease-snappy);
}
@keyframes zd-chart-rise {
  0%   { transform: scaleY(0); transform-origin: bottom; opacity: 0; }
  100% { transform: scaleY(1); transform-origin: bottom; opacity: 1; }
}
@keyframes zd-chart-tip-in {
  0%   { opacity: 0; transform: translate(-50%, 4px); }
  100% { opacity: 1; transform: translate(-50%, 0); }
}
</style>
