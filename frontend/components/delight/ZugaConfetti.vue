<script setup lang="ts">
import { ref, watch, computed } from 'vue'

const props = withDefaults(defineProps<{
  trigger?: boolean | number
  intensity?: 'light' | 'medium' | 'heavy'
}>(), {
  trigger: false,
  intensity: 'medium',
})

const COUNTS: Record<string, number> = { light: 18, medium: 36, heavy: 64 }

type Particle = {
  id: number
  x: number; y: number
  dx: number; dy: number
  rot: number; rotEnd: number
  hue: 'a' | 'b'
  delay: number
}

const particles = ref<Particle[]>([])
const playing = ref(false)
const seq = ref(0)

const spawn = () => {
  const n = COUNTS[props.intensity] ?? 36
  const list: Particle[] = []
  for (let i = 0; i < n; i++) {
    const angle = (Math.PI * 2 * i) / n + Math.random() * 0.4
    const speed = 80 + Math.random() * 140
    list.push({
      id: i,
      x: 50, y: 50,
      dx: Math.cos(angle) * speed,
      dy: Math.sin(angle) * speed - 20,
      rot: Math.random() * 360,
      rotEnd: Math.random() * 720 - 360,
      hue: i % 2 === 0 ? 'a' : 'b',
      delay: Math.random() * 80,
    })
  }
  particles.value = list
  playing.value = true
  window.setTimeout(() => { playing.value = false; particles.value = [] }, 1400)
}

watch(() => props.trigger, () => { seq.value++; spawn() })

const styleFor = (p: Particle) => ({
  '--zd-c-dx': `${p.dx}px`,
  '--zd-c-dy': `${p.dy}px`,
  '--zd-c-rot0': `${p.rot}deg`,
  '--zd-c-rot1': `${p.rotEnd}deg`,
  animationDelay: `${p.delay}ms`,
})
</script>

<template>
  <div v-if="playing" class="zd-confetti" aria-hidden="true">
    <span
      v-for="p in particles"
      :key="`${seq}-${p.id}`"
      class="zd-confetti-piece"
      :class="[`hue-${p.hue}`]"
      :style="styleFor(p)"
    />
  </div>
</template>

<style scoped>
.zd-confetti {
  position: absolute;
  inset: 0;
  pointer-events: none;
  overflow: visible;
  z-index: 50;
}
.zd-confetti-piece {
  position: absolute;
  left: 50%;
  top: 50%;
  width: 8px;
  height: 12px;
  border-radius: 2px;
  transform: translate(-50%, -50%) rotate(var(--zd-c-rot0));
  animation: zd-confetti-fly var(--zd-dur-arrival) var(--zd-ease-celebrate) forwards;
}
.zd-confetti-piece.hue-a { background: rgb(var(--accent)); }
.zd-confetti-piece.hue-b { background: rgb(var(--accent-bright, var(--accent))); }

@keyframes zd-confetti-fly {
  0%   { opacity: 1; transform: translate(-50%, -50%) rotate(var(--zd-c-rot0)); }
  100% {
    opacity: 0;
    transform:
      translate(calc(-50% + var(--zd-c-dx)), calc(-50% + var(--zd-c-dy)))
      rotate(var(--zd-c-rot1));
  }
}
</style>
