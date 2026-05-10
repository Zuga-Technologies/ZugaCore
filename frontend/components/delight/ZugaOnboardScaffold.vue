<script setup lang="ts">
import { ref, computed, watch } from 'vue'

const props = withDefaults(defineProps<{
  step: number
  total: number
}>(), {
  step: 0,
  total: 1,
})

const emit = defineEmits<{ (e: 'complete'): void }>()

const direction = ref<'forward' | 'back'>('forward')
const lastStep = ref(props.step)

watch(() => props.step, (n, o) => {
  direction.value = n > o ? 'forward' : 'back'
  lastStep.value = n
  if (n >= props.total) emit('complete')
})

const pct = computed(() => Math.min(1, (props.step + 1) / props.total))
</script>

<template>
  <section class="zd-onboard" :data-direction="direction">
    <header class="zd-onboard-head">
      <div class="zd-onboard-progress">
        <div
          class="zd-onboard-progress-fill"
          :style="{ width: `${pct * 100}%` }"
        />
      </div>
      <div class="zd-onboard-step">
        <span>{{ Math.min(step + 1, total) }} / {{ total }}</span>
      </div>
    </header>

    <Transition name="zd-step" mode="out-in">
      <div :key="step" class="zd-onboard-step-body">
        <slot />
      </div>
    </Transition>

    <footer class="zd-onboard-foot">
      <slot name="actions" />
    </footer>
  </section>
</template>

<style scoped>
.zd-onboard {
  display: flex;
  flex-direction: column;
  gap: 24px;
  min-height: 100%;
  padding: 24px;
  background: rgb(var(--surface-1));
  border-radius: 16px;
  animation: zd-onboard-arrive var(--zd-dur-arrival) var(--zd-ease-soft) both;
}
.zd-onboard-head {
  display: flex;
  align-items: center;
  gap: 12px;
}
.zd-onboard-progress {
  flex: 1;
  height: 4px;
  border-radius: 999px;
  background: rgba(var(--accent), 0.15);
  overflow: hidden;
}
.zd-onboard-progress-fill {
  height: 100%;
  background: linear-gradient(
    90deg,
    rgb(var(--accent)) 0%,
    rgb(var(--accent-bright, var(--accent))) 100%
  );
  transition: width var(--zd-dur-feature) var(--zd-ease-soft);
}
.zd-onboard-step {
  font-size: 12px;
  color: rgb(var(--txt-muted));
  font-variant-numeric: tabular-nums;
}
.zd-onboard-step-body {
  flex: 1;
}
.zd-onboard-foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

/* Forward = slide left+fade. Back = slide right+fade. */
.zd-step-enter-active,
.zd-step-leave-active {
  transition:
    opacity var(--zd-dur-base) var(--zd-ease-soft),
    transform var(--zd-dur-base) var(--zd-ease-soft);
}
[data-direction="forward"] .zd-step-enter-from { opacity: 0; transform: translateX( 16px); }
[data-direction="forward"] .zd-step-leave-to   { opacity: 0; transform: translateX(-16px); }
[data-direction="back"]    .zd-step-enter-from { opacity: 0; transform: translateX(-16px); }
[data-direction="back"]    .zd-step-leave-to   { opacity: 0; transform: translateX( 16px); }

@keyframes zd-onboard-arrive {
  0%   { opacity: 0; transform: translateY(8px); }
  100% { opacity: 1; transform: translateY(0); }
}
</style>
