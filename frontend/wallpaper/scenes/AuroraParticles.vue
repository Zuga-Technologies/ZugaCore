<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'

const canvas = ref<HTMLCanvasElement | null>(null)
let raf: number | null = null

let mouseX = -9999
let mouseY = -9999
// Tuned to match the ZugaTechnologies ParticleField "feel": large radius,
// tiny per-frame velocity nudge, integrated by physics rather than slapped
// onto the draw position. Smoothness comes from damping + integration.
const MOUSE_RADIUS = 220
const MOUSE_RADIUS_SQ = MOUSE_RADIUS * MOUSE_RADIUS
// Subtle attraction toward the cursor — not a strong pull.
// Slightly stronger than the ZugaTechnologies hero because the aurora overlay
// dims particle contrast, so the same numerical pull reads as quieter here.
const MOUSE_ATTRACT = 0.022      // per-frame velocity nudge (toward cursor)
const VELOCITY_DAMPING = 0.985   // glide-out after mouse leaves
const MIN_SPEED = 0.04           // floor so particles don't stall
const SEED_MAX_SPEED = 0.10      // upper bound on initial seed speed only

interface Particle {
  x: number
  y: number
  vx: number
  vy: number
  r: number
  hue: number
  alpha: number
}

const particles: Particle[] = []
let viewW = 0
let viewH = 0
let dpr = 1

// Adaptive count: ~1 particle per 25k CSS pixels, capped to keep mobile fluid.
function targetCount(w: number, h: number): number {
  return Math.max(24, Math.min(80, Math.round((w * h) / 25000)))
}

function makeParticle(w: number, h: number): Particle {
  // Seed with a small directional velocity (uniformly-distributed angle,
  // moderate speed) — gives clean drift instead of axis-biased jitter.
  const a = Math.random() * Math.PI * 2
  const s = MIN_SPEED + Math.random() * (SEED_MAX_SPEED - MIN_SPEED)
  return {
    x: Math.random() * w,
    y: Math.random() * h,
    vx: Math.cos(a) * s,
    vy: Math.sin(a) * s,
    r: Math.random() * 2 + 0.5,
    hue: 180 + Math.random() * 120,
    alpha: Math.random() * 0.5 + 0.2,
  }
}

function reconcileParticles(w: number, h: number) {
  const want = targetCount(w, h)
  // Reposition out-of-bounds particles instead of full re-seed.
  for (const p of particles) {
    if (p.x < 0 || p.x > w) p.x = Math.random() * w
    if (p.y < 0 || p.y > h) p.y = Math.random() * h
  }
  while (particles.length < want) particles.push(makeParticle(w, h))
  if (particles.length > want) particles.length = want
}

// Cached aurora gradient — rebuilt every N frames or on resize.
let auroraGrad: CanvasGradient | null = null
let auroraGradFrame = 0
const AURORA_REBUILD_EVERY = 8

function buildAuroraGrad(ctx: CanvasRenderingContext2D, w: number, h: number, time: number) {
  const auroraY = h * 0.4 + Math.sin(time * 0.3) * 30
  const g = ctx.createLinearGradient(0, auroraY - 200, 0, auroraY + 200)
  g.addColorStop(0, 'rgba(56, 189, 248, 0)')
  g.addColorStop(0.5, `rgba(${100 + Math.sin(time) * 50}, ${200 + Math.cos(time * 0.7) * 30}, 255, 0.18)`)
  g.addColorStop(1, 'rgba(167, 139, 250, 0)')
  return g
}

let bgGrad: CanvasGradient | null = null
let bgGradFrame = 0
const BG_REBUILD_EVERY = 16

function buildBgGrad(ctx: CanvasRenderingContext2D, w: number, h: number, time: number) {
  const t1 = (Math.sin(time * 0.1) + 1) / 2
  const t2 = (Math.cos(time * 0.13) + 1) / 2
  const g = ctx.createLinearGradient(0, 0, w, h)
  g.addColorStop(0, `hsl(${260 + t1 * 30}, 60%, ${8 + t1 * 4}%)`)
  g.addColorStop(0.5, `hsl(${220 + t2 * 40}, 70%, ${10 + t2 * 6}%)`)
  g.addColorStop(1, `hsl(${190 + t1 * 30}, 80%, ${6 + t2 * 4}%)`)
  return g
}

function animate() {
  const c = canvas.value
  if (!c) return
  const ctx = c.getContext('2d')
  if (!ctx) return

  // Skip work entirely when the tab is hidden — RAF already throttles, but
  // this avoids any wasted gradient/path allocations when it does fire.
  if (document.hidden) {
    raf = requestAnimationFrame(animate)
    return
  }

  const w = viewW
  const h = viewH
  const time = performance.now() / 1000

  if (!bgGrad || bgGradFrame <= 0) {
    bgGrad = buildBgGrad(ctx, w, h, time)
    bgGradFrame = BG_REBUILD_EVERY
  }
  bgGradFrame--
  ctx.fillStyle = bgGrad
  ctx.fillRect(0, 0, w, h)

  if (!auroraGrad || auroraGradFrame <= 0) {
    auroraGrad = buildAuroraGrad(ctx, w, h, time)
    auroraGradFrame = AURORA_REBUILD_EVERY
  }
  auroraGradFrame--
  ctx.fillStyle = auroraGrad
  ctx.fillRect(0, 0, w, h)

  // Slower twinkle frequency — visual noise was contributing to "jittery" feel.
  const timeTwinkle = time * 0.9
  for (const p of particles) {
    // Subtle mouse attraction — applied to velocity, not draw offset. Physics
    // integration is what makes the motion feel silky.
    const mdx = mouseX - p.x
    const mdy = mouseY - p.y
    const distSq = mdx * mdx + mdy * mdy
    if (distSq < MOUSE_RADIUS_SQ && distSq > 0.0001) {
      const dist = Math.sqrt(distSq)
      const falloff = (MOUSE_RADIUS - dist) / MOUSE_RADIUS
      p.vx += (mdx / dist) * falloff * MOUSE_ATTRACT
      p.vy += (mdy / dist) * falloff * MOUSE_ATTRACT
    }

    // Damp + min-speed floor only — no max clamp. Cursor force accumulates
    // freely while the mouse is engaged and naturally damps back when it leaves.
    p.vx *= VELOCITY_DAMPING
    p.vy *= VELOCITY_DAMPING
    const sp = Math.sqrt(p.vx * p.vx + p.vy * p.vy)
    if (sp < MIN_SPEED) {
      const a = sp > 0 ? Math.atan2(p.vy, p.vx) : Math.random() * Math.PI * 2
      p.vx = Math.cos(a) * MIN_SPEED
      p.vy = Math.sin(a) * MIN_SPEED
    }

    p.x += p.vx
    p.y += p.vy
    if (p.x < -5) p.x = w + 5
    if (p.x > w + 5) p.x = -5
    if (p.y < -5) p.y = h + 5
    if (p.y > h + 5) p.y = -5

    const twinkle = (Math.sin(timeTwinkle + p.x * 0.01) + 1) / 2
    const a = p.alpha * (0.5 + twinkle * 0.5)
    ctx.fillStyle = `hsla(${p.hue}, 80%, 70%, ${a})`
    ctx.beginPath()
    ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2)
    ctx.fill()

    ctx.fillStyle = `hsla(${p.hue}, 80%, 70%, ${p.alpha * 0.15})`
    ctx.beginPath()
    ctx.arc(p.x, p.y, p.r * 4, 0, Math.PI * 2)
    ctx.fill()
  }

  raf = requestAnimationFrame(animate)
}

let resizeRaf = 0
function scheduleResize() {
  if (resizeRaf) return
  resizeRaf = requestAnimationFrame(() => {
    resizeRaf = 0
    resize()
  })
}

function resize() {
  const c = canvas.value
  if (!c) return
  dpr = Math.min(window.devicePixelRatio || 1, 2)
  viewW = window.innerWidth
  viewH = window.innerHeight
  c.width = Math.max(1, Math.floor(viewW * dpr))
  c.height = Math.max(1, Math.floor(viewH * dpr))
  c.style.width = viewW + 'px'
  c.style.height = viewH + 'px'
  const ctx = c.getContext('2d')
  if (ctx) ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  reconcileParticles(viewW, viewH)
  // Invalidate cached gradients — dimensions changed.
  bgGrad = null
  auroraGrad = null
}

function onMouseMove(e: MouseEvent) {
  mouseX = e.clientX
  mouseY = e.clientY
}

function onMouseLeave() {
  mouseX = -9999
  mouseY = -9999
}

// Static fallback for prefers-reduced-motion: paint once, no RAF loop.
function paintStatic() {
  const c = canvas.value
  if (!c) return
  const ctx = c.getContext('2d')
  if (!ctx) return
  const w = viewW
  const h = viewH
  const g = ctx.createLinearGradient(0, 0, w, h)
  g.addColorStop(0, 'hsl(265, 60%, 10%)')
  g.addColorStop(0.5, 'hsl(225, 70%, 12%)')
  g.addColorStop(1, 'hsl(195, 80%, 8%)')
  ctx.fillStyle = g
  ctx.fillRect(0, 0, w, h)
}

let reduceMotion = false
let motionMql: MediaQueryList | null = null

function onMotionChange() {
  reduceMotion = !!motionMql?.matches
  if (reduceMotion) {
    if (raf) { cancelAnimationFrame(raf); raf = null }
    paintStatic()
  } else if (!raf) {
    raf = requestAnimationFrame(animate)
  }
}

onMounted(() => {
  motionMql = window.matchMedia('(prefers-reduced-motion: reduce)')
  reduceMotion = motionMql.matches
  motionMql.addEventListener('change', onMotionChange)

  resize()
  window.addEventListener('resize', scheduleResize)
  window.addEventListener('mousemove', onMouseMove, { passive: true })
  document.addEventListener('mouseleave', onMouseLeave)

  if (reduceMotion) {
    paintStatic()
  } else {
    raf = requestAnimationFrame(animate)
  }
})

onUnmounted(() => {
  if (raf) cancelAnimationFrame(raf)
  if (resizeRaf) cancelAnimationFrame(resizeRaf)
  motionMql?.removeEventListener('change', onMotionChange)
  window.removeEventListener('resize', scheduleResize)
  window.removeEventListener('mousemove', onMouseMove)
  document.removeEventListener('mouseleave', onMouseLeave)
})
</script>

<template>
  <canvas ref="canvas" class="aurora-canvas" />
</template>

<style scoped>
.aurora-canvas {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  display: block;
}
</style>
