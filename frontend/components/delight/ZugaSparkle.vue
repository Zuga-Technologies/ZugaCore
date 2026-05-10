<script setup lang="ts">
import { ref, watch } from 'vue'

const props = withDefaults(defineProps<{
  trigger?: boolean | number
  size?: number
}>(), {
  trigger: false,
  size: 24,
})

const playing = ref(false)
const key = ref(0)

watch(() => props.trigger, () => {
  key.value++
  playing.value = true
  window.setTimeout(() => { playing.value = false }, 700)
})
</script>

<template>
  <span class="zd-sparkle" :style="{ width: `${size}px`, height: `${size}px` }">
    <svg
      v-if="playing"
      :key="key"
      viewBox="0 0 24 24"
      class="zd-sparkle-svg"
      aria-hidden="true"
    >
      <g stroke="rgb(var(--accent))" stroke-width="1.6" stroke-linecap="round">
        <line x1="12" y1="2"  x2="12" y2="7"  class="zd-ray r0" />
        <line x1="12" y1="17" x2="12" y2="22" class="zd-ray r1" />
        <line x1="2"  y1="12" x2="7"  y2="12" class="zd-ray r2" />
        <line x1="17" y1="12" x2="22" y2="12" class="zd-ray r3" />
        <line x1="5"  y1="5"  x2="8.5"  y2="8.5"  class="zd-ray r4" />
        <line x1="15.5" y1="15.5" x2="19" y2="19" class="zd-ray r5" />
        <line x1="19" y1="5"  x2="15.5" y2="8.5"  class="zd-ray r6" />
        <line x1="8.5" y1="15.5" x2="5" y2="19" class="zd-ray r7" />
      </g>
      <circle cx="12" cy="12" r="2" fill="rgb(var(--accent))" class="zd-core" />
    </svg>
  </span>
</template>

<style scoped>
.zd-sparkle {
  display: inline-block;
  position: relative;
  pointer-events: none;
}
.zd-sparkle-svg {
  width: 100%;
  height: 100%;
}
.zd-core {
  transform-origin: center;
  animation: zd-core-pop var(--zd-dur-feature) var(--zd-ease-celebrate) forwards;
}
.zd-ray {
  transform-origin: 12px 12px;
  opacity: 0;
  animation: zd-ray-fly var(--zd-dur-feature) var(--zd-ease-celebrate) forwards;
}
.zd-ray.r0 { animation-delay:  0ms; }
.zd-ray.r1 { animation-delay: 30ms; }
.zd-ray.r2 { animation-delay: 60ms; }
.zd-ray.r3 { animation-delay: 90ms; }
.zd-ray.r4 { animation-delay: 45ms; }
.zd-ray.r5 { animation-delay: 75ms; }
.zd-ray.r6 { animation-delay: 15ms; }
.zd-ray.r7 { animation-delay:105ms; }

@keyframes zd-core-pop {
  0%   { transform: scale(0); }
  60%  { transform: scale(1.4); }
  100% { transform: scale(0.9); }
}
@keyframes zd-ray-fly {
  0%   { opacity: 0; transform: scale(0.3); }
  40%  { opacity: 1; transform: scale(1); }
  100% { opacity: 0; transform: scale(1.25); }
}
</style>
