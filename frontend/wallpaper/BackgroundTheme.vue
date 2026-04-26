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

// Widened to string so user-theme ids ('th_*') are accepted alongside the 4 hardcoded ThemeIds
const currentTheme = ref<string>(getSavedTheme())
const isUserTheme = computed(() => currentTheme.value.startsWith('th_'))
// Only resolve through getTheme when it's a known hardcoded id — user-theme ids are not in the registry
const theme = computed(() => getTheme((isUserTheme.value ? 'none' : currentTheme.value) as ThemeId))
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

// User-theme types (from Plan 4a schemas)
interface UserTheme {
  id: string
  name: string
  preview_color: string | null
  active_wallpaper_id: string | null
  rotation_interval_minutes: number
}
interface UserWallpaper {
  id: string
  source_studio: 'image' | 'video'
  source_id: string
  kind: 'image' | 'video'
  name: string | null
}

// AI Ambient wallpaper state
const aiCurrentImage = ref<string | null>(null)
const aiPreviousImage = ref<string | null>(null)
const aiShowCurrent = ref(true)
let aiPollTimer: ReturnType<typeof setInterval> | null = null
const isAIAmbient = computed(() => currentTheme.value === 'ai-ambient')

// User-theme wallpaper state (rotate-or-pin)
const userThemeData = ref<UserTheme | null>(null)
const userWallpapers = ref<UserWallpaper[]>([])
const userCurrentIndex = ref(0)
const userCurrentUrl = ref<string | null>(null)
const userPreviousUrl = ref<string | null>(null)
const userCurrentKind = ref<'image' | 'video'>('image')
const userPreviousKind = ref<'image' | 'video' | null>(null)
const userShowCurrent = ref(true)
let userRotateTimer: ReturnType<typeof setInterval> | null = null
// Incremented by stopUserTheme() to invalidate any in-flight startUserTheme fetches
let userThemeReqVersion = 0

function buildWallpaperUrl(w: UserWallpaper): string {
  // Switch on source_studio (which studio owns this asset) — kind drives template branching
  if (w.source_studio === 'video') return `/api/video/wallpaper/${w.source_id}/download`
  return `/api/image/saved/${w.source_id}`  // source_studio === 'image'
}

async function startUserTheme(themeId: string) {
  stopUserTheme()
  const myVersion = ++userThemeReqVersion
  let themeData: UserTheme
  let wallpapers: UserWallpaper[]
  try {
    themeData = await api.get<UserTheme>(`/api/themes/${themeId}`)
    if (myVersion !== userThemeReqVersion) return  // superseded by a newer call
    wallpapers = await api.get<UserWallpaper[]>(`/api/wallpapers/mine?theme_id=${themeId}`)
    if (myVersion !== userThemeReqVersion) return  // superseded by a newer call
  } catch (err) {
    console.warn('[BackgroundTheme] failed to load user theme', themeId, err)
    return
  }
  userThemeData.value = themeData
  userWallpapers.value = wallpapers
  if (userWallpapers.value.length === 0) return

  const pinId = userThemeData.value?.active_wallpaper_id
  if (pinId) {
    const pinned = userWallpapers.value.find(w => w.id === pinId)
    if (pinned) {
      userCurrentUrl.value = buildWallpaperUrl(pinned)
      userCurrentKind.value = pinned.kind
      return  // pinned: no rotation timer needed
    }
    // pinned wallpaper deleted/missing — fall through to rotation
  }

  // Rotation mode
  userCurrentIndex.value = 0
  const first = userWallpapers.value[0]
  userCurrentUrl.value = buildWallpaperUrl(first)
  userCurrentKind.value = first.kind
  const intervalMs = (userThemeData.value?.rotation_interval_minutes ?? 30) * 60 * 1000
  if (userWallpapers.value.length > 1) {
    userRotateTimer = setInterval(() => {
      userPreviousUrl.value = userCurrentUrl.value
      userPreviousKind.value = userCurrentKind.value
      userCurrentIndex.value = (userCurrentIndex.value + 1) % userWallpapers.value.length
      const next = userWallpapers.value[userCurrentIndex.value]
      userCurrentUrl.value = buildWallpaperUrl(next)
      userCurrentKind.value = next.kind
      userShowCurrent.value = false
      requestAnimationFrame(() => { userShowCurrent.value = true })
    }, intervalMs)
  }
}

function stopUserTheme() {
  userThemeReqVersion++  // invalidate any in-flight startUserTheme fetches
  if (userRotateTimer) { clearInterval(userRotateTimer); userRotateTimer = null }
  userThemeData.value = null
  userWallpapers.value = []
  userCurrentUrl.value = null
  userPreviousUrl.value = null
  userCurrentKind.value = 'image'
  userPreviousKind.value = null
  userShowCurrent.value = true
}

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
  if (id.startsWith('th_')) {
    stopAIAmbient()
    startUserTheme(id)
  } else if (id === 'ai-ambient') {
    stopUserTheme()
    startAIAmbient()
  } else if (id === 'custom') {
    stopAIAmbient()
    stopUserTheme()
    loadCustomVideo()
  } else {
    stopAIAmbient()
    stopUserTheme()
    const t = getTheme(id as ThemeId)
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
  if (currentTheme.value.startsWith('th_')) {
    startUserTheme(currentTheme.value)
  } else if (currentTheme.value === 'ai-ambient') {
    startAIAmbient()
  } else if (currentTheme.value === 'custom') {
    loadCustomVideo()
  } else if (theme.value.video && !prefersReducedMotion.value && videoA.value) {
    loadVideo(theme.value.video)
  }
})

onUnmounted(() => {
  stopAIAmbient()
  stopUserTheme()
})

// Listen for theme change events from settings panel.
// Cast to string (not ThemeId) — user-theme ids like 'th_*' are valid here.
function handleThemeChange(e: Event) {
  currentTheme.value = (e as CustomEvent<string>).detail
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

    <!-- User Theme wallpaper (rotate-or-pin, crossfade between two image/video layers) -->
    <template v-if="isUserTheme">
      <!-- Previous (video) -->
      <video
        v-if="userPreviousUrl && userPreviousKind === 'video'"
        :key="`prev-${userPreviousUrl}`"
        :src="userPreviousUrl"
        class="absolute inset-0 w-full h-full object-cover transition-opacity duration-[3000ms]"
        :class="userShowCurrent ? 'opacity-0' : 'opacity-100'"
        autoplay loop muted playsinline
      />
      <!-- Previous (image) -->
      <div
        v-else-if="userPreviousUrl"
        :key="`prev-${userPreviousUrl}`"
        class="absolute inset-0 bg-cover bg-center bg-no-repeat transition-opacity duration-[3000ms]"
        :class="userShowCurrent ? 'opacity-0' : 'opacity-100'"
        :style="{ backgroundImage: `url(${userPreviousUrl})` }"
      />
      <!-- Current (video) -->
      <video
        v-if="userCurrentUrl && userCurrentKind === 'video'"
        :key="`curr-${userCurrentUrl}`"
        :src="userCurrentUrl"
        class="absolute inset-0 w-full h-full object-cover transition-opacity duration-[3000ms]"
        :class="userShowCurrent ? 'opacity-100' : 'opacity-0'"
        autoplay loop muted playsinline
      />
      <!-- Current (image) -->
      <div
        v-else-if="userCurrentUrl"
        :key="`curr-${userCurrentUrl}`"
        class="absolute inset-0 bg-cover bg-center bg-no-repeat transition-opacity duration-[3000ms]"
        :class="userShowCurrent ? 'opacity-100' : 'opacity-0'"
        :style="{ backgroundImage: `url(${userCurrentUrl})` }"
      />
      <!-- Subtle dark overlay so UI text stays readable over bright wallpapers -->
      <div
        class="absolute inset-0"
        style="background: rgba(0, 0, 0, 0.2)"
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
