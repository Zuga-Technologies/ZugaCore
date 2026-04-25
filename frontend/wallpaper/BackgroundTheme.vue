<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch, nextTick, computed } from 'vue'
import { api } from '@core/api/client'
import {
  type ThemeId,
  getSavedTheme,
  getTheme,
  getCustomImage,
  getCustomOpacity,
  getCustomVideo,
  getCustomVideoSpeed,
  getCustomMediaType,
  getAIAmbientTheme,
  getAIAmbientInterval,
} from './registry'

const currentTheme = ref<ThemeId>(getSavedTheme())
const theme = computed(() => getTheme(currentTheme.value))
const hasVideo = computed(() => !!theme.value.video)
const DEFAULT_SPEED = 0.5

// Video refs for crossfade loop
const videoA = ref<HTMLVideoElement | null>(null)
const videoB = ref<HTMLVideoElement | null>(null)
const activeVideo = ref<'a' | 'b'>('a')
const videoLoaded = ref(false)

// Custom video
const customVideoRef = ref<HTMLVideoElement | null>(null)
const customVideoSrc = ref<string | null>(null)
const isCustomVideo = computed(() => currentTheme.value === 'custom' && getCustomMediaType() === 'video')

// AI Ambient wallpaper state
const aiCurrentImage = ref<string | null>(null)
const aiPreviousImage = ref<string | null>(null)
const aiShowCurrent = ref(true)
let aiPollTimer: ReturnType<typeof setInterval> | null = null
const isAIAmbient = computed(() => currentTheme.value === 'ai-ambient')

// Prefers-reduced-motion
const prefersReducedMotion = ref(false)
let motionQuery: MediaQueryList | null = null

onMounted(() => {
  motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)')
  prefersReducedMotion.value = motionQuery.matches
  motionQuery.addEventListener('change', (e) => { prefersReducedMotion.value = e.matches })
})

// Force playback rate — browsers reset it on load/play/loop
function enforceRate(video: HTMLVideoElement, rate?: number) {
  const target = rate ?? theme.value.speed ?? DEFAULT_SPEED
  if (video.playbackRate !== target) {
    video.playbackRate = target
  }
}

function handleRateEnforce(which: 'a' | 'b') {
  const video = which === 'a' ? videoA.value : videoB.value
  if (video) enforceRate(video)
}

// Crossfade: when active video nears end, fade to the other
// Throttled — timeupdate fires ~4x/sec, we only need ~4 checks/sec near the end
let lastTimeUpdate = 0
const videoBLoaded = ref(false)

function handleTimeUpdate(which: 'a' | 'b') {
  const now = performance.now()
  if (now - lastTimeUpdate < 250) return
  lastTimeUpdate = now

  const video = which === 'a' ? videoA.value : videoB.value
  const other = which === 'a' ? videoB.value : videoA.value
  if (!video || !other || activeVideo.value !== which) return

  enforceRate(video)

  const remaining = video.duration - video.currentTime

  // Lazy-load video B: only set its src when A is ~5s from ending
  if (!videoBLoaded.value && remaining < 5 && remaining > 0 && theme.value.video) {
    loadVideoB(theme.value.video)
  }

  if (remaining < 1.5 && remaining > 0 && videoBLoaded.value) {
    other.currentTime = 0
    other.play().catch(() => {})
    enforceRate(other)
    activeVideo.value = which === 'a' ? 'b' : 'a'
  }
}

// When theme changes, reset videos — only load A immediately, B is lazy
function loadVideo(mp4Src: string) {
  videoLoaded.value = false
  videoBLoaded.value = false
  activeVideo.value = 'a'
  nextTick(() => {
    if (videoA.value) {
      // Set .src directly — <source> tags have race conditions with Vue reactivity
      const webmSrc = theme.value.videoWebm
      if (webmSrc && videoA.value.canPlayType('video/webm; codecs="vp9"')) {
        videoA.value.src = webmSrc
      } else {
        videoA.value.src = mp4Src
      }
      videoA.value.load()
      videoA.value.playbackRate = theme.value.speed ?? DEFAULT_SPEED
      videoA.value.play().catch(() => {})
    }
    // Video B is NOT loaded here — lazy-loaded in handleTimeUpdate when A nears end
  })
}

// Lazy-load video B just before it's needed — prefer WebM if available
function loadVideoB(mp4Src: string) {
  if (videoBLoaded.value || !videoB.value) return
  const webmSrc = theme.value.videoWebm
  if (webmSrc && videoB.value.canPlayType('video/webm; codecs="vp9"')) {
    videoB.value.src = webmSrc
  } else {
    videoB.value.src = mp4Src
  }
  videoB.value.load()
  videoB.value.playbackRate = theme.value.speed ?? DEFAULT_SPEED
  videoBLoaded.value = true
}

// Custom video: enforce user-chosen speed (throttled to avoid per-frame overhead)
let lastCustomRateCheck = 0
function enforceCustomVideoRate() {
  const now = performance.now()
  if (now - lastCustomRateCheck < 1000) return
  lastCustomRateCheck = now
  if (customVideoRef.value) {
    enforceRate(customVideoRef.value, getCustomVideoSpeed())
  }
}

// Load custom video from IndexedDB
async function loadCustomVideo() {
  if (getCustomMediaType() !== 'video') {
    customVideoSrc.value = null
    return
  }
  const url = await getCustomVideo()
  customVideoSrc.value = url
  if (url) {
    nextTick(() => {
      if (customVideoRef.value) {
        customVideoRef.value.play().catch(() => {})
      }
    })
  }
}

// AI Ambient: start/stop scheduler and poll for new wallpapers
async function startAIAmbient() {
  const ambientTheme = getAIAmbientTheme()
  const interval = getAIAmbientInterval()

  try {
    // Start the ambient scheduler in ZugaVideo
    const data = await api.post<{ current_wallpaper_data?: string }>('/api/video/ambient/start', {
      theme: ambientTheme,
      interval_minutes: interval,
      size: '1920x1080',
      provider: 'flux_schnell',
    })
    if (data?.current_wallpaper_data) {
      aiCurrentImage.value = data.current_wallpaper_data
    }
  } catch { /* ZugaVideo may not be loaded */ }

  // Poll for updates
  aiPollTimer = setInterval(async () => {
    try {
      const data = await api.get<{ current_wallpaper_data?: string; current_wallpaper_url?: string }>('/api/video/ambient/status')
      const newImage = data.current_wallpaper_data || data.current_wallpaper_url
      if (newImage && newImage !== aiCurrentImage.value) {
        aiPreviousImage.value = aiCurrentImage.value
        aiCurrentImage.value = newImage
        aiShowCurrent.value = false
        requestAnimationFrame(() => { aiShowCurrent.value = true })
      }
    } catch { /* silent */ }
  }, 15000)
}

function stopAIAmbient() {
  if (aiPollTimer) {
    clearInterval(aiPollTimer)
    aiPollTimer = null
  }
  // Don't stop the scheduler — other tabs may be using it
}

watch(currentTheme, (id) => {
  const t = getTheme(id)
  if (id === 'ai-ambient') {
    startAIAmbient()
  } else if (id === 'custom') {
    stopAIAmbient()
    loadCustomVideo()
  } else {
    stopAIAmbient()
    if (t.video && !prefersReducedMotion.value) {
      loadVideo(t.video)
    }
  }
}, { immediate: false })

// Initial load — watch videoA ref to ensure the element exists in DOM before loading
// (v-if="hasVideo" means the <video> may not be rendered on first onMounted tick)
watch(videoA, (el) => {
  if (el && theme.value.video && !prefersReducedMotion.value && !videoLoaded.value) {
    loadVideo(theme.value.video)
  }
})

onMounted(() => {
  if (currentTheme.value === 'ai-ambient') {
    startAIAmbient()
  } else if (currentTheme.value === 'custom') {
    loadCustomVideo()
  } else if (theme.value.video && !prefersReducedMotion.value && videoA.value) {
    loadVideo(theme.value.video)
  }
})

onUnmounted(() => {
  stopAIAmbient()
})

// Listen for theme change events from settings panel
function handleThemeChange(e: Event) {
  currentTheme.value = (e as CustomEvent).detail as ThemeId
}
onMounted(() => document.addEventListener('zugalife-theme-change', handleThemeChange))
onUnmounted(() => document.removeEventListener('zugalife-theme-change', handleThemeChange))
</script>

<template>
  <div class="fixed inset-0 z-0 overflow-hidden">
    <!-- Base layer: fallback gradient or solid dark -->
    <div
      class="absolute inset-0 transition-[background] duration-1000"
      :style="{
        background: theme.fallbackBg || '#0a0a0a',
      }"
    />

    <!-- Preset video layers (A and B for crossfade) -->
    <template v-if="hasVideo && !isCustomVideo && !prefersReducedMotion">
      <video
        ref="videoA"
        class="absolute inset-0 w-full h-full object-cover transition-opacity duration-[1500ms] will-change-[opacity]"
        :class="activeVideo === 'a' ? 'opacity-100' : 'opacity-0'"
        :poster="theme.poster"
        autoplay
        loop
        muted
        playsinline
        @loadeddata="videoLoaded = true; handleRateEnforce('a')"
        @playing="handleRateEnforce('a')"
        @timeupdate="handleTimeUpdate('a')"
      />
      <video
        ref="videoB"
        class="absolute inset-0 w-full h-full object-cover transition-opacity duration-[1500ms] will-change-[opacity]"
        :class="activeVideo === 'b' ? 'opacity-100' : 'opacity-0'"
        preload="none"
        loop
        muted
        playsinline
        @loadeddata="handleRateEnforce('b')"
        @playing="handleRateEnforce('b')"
        @timeupdate="handleTimeUpdate('b')"
      />
    </template>

    <!-- Dark overlay for preset videos -->
    <div
      v-if="hasVideo && !isCustomVideo && theme.overlay && theme.overlay > 0"
      class="absolute inset-0"
      :style="{ background: `rgba(0, 0, 0, ${theme.overlay})` }"
    />

    <!-- AI Ambient wallpaper (crossfade between two images) -->
    <template v-if="isAIAmbient">
      <div
        v-if="aiPreviousImage"
        class="absolute inset-0 bg-cover bg-center bg-no-repeat transition-opacity duration-[3000ms]"
        :class="aiShowCurrent ? 'opacity-0' : 'opacity-100'"
        :style="{ backgroundImage: `url(${aiPreviousImage})` }"
      />
      <div
        v-if="aiCurrentImage"
        class="absolute inset-0 bg-cover bg-center bg-no-repeat transition-opacity duration-[3000ms]"
        :class="aiShowCurrent ? 'opacity-100' : 'opacity-0'"
        :style="{ backgroundImage: `url(${aiCurrentImage})` }"
      />
      <!-- Dark overlay for AI wallpapers -->
      <div
        class="absolute inset-0"
        :style="{ background: `rgba(0, 0, 0, ${theme.overlay || 0.25})` }"
      />
    </template>

    <!-- Custom Image -->
    <div
      v-if="currentTheme === 'custom' && getCustomMediaType() === 'image' && getCustomImage()"
      class="absolute inset-0 bg-cover bg-center bg-no-repeat"
      :style="{
        backgroundImage: `url(${getCustomImage()})`,
        opacity: getCustomOpacity(),
      }"
    />

    <!-- Custom Video -->
    <template v-if="isCustomVideo && customVideoSrc && !prefersReducedMotion">
      <video
        ref="customVideoRef"
        :src="customVideoSrc"
        class="absolute inset-0 w-full h-full object-cover"
        autoplay
        loop
        muted
        playsinline
        @loadeddata="enforceCustomVideoRate"
        @playing="enforceCustomVideoRate"
        @timeupdate="enforceCustomVideoRate"
      />
      <!-- Dim overlay for custom video -->
      <div
        class="absolute inset-0"
        :style="{ background: `rgba(0, 0, 0, ${getCustomOpacity()})` }"
      />
    </template>
  </div>
</template>
