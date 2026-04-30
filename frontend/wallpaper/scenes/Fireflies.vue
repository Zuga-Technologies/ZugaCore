<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'

const canvas = ref<HTMLCanvasElement | null>(null)
let raf: number | null = null

let mouseX = -9999
let mouseY = -9999
let smoothMouseX = -9999
let smoothMouseY = -9999
const MOUSE_FOLLOW = 0.15

// Fireflies are calmer than aurora particles — gentle attract only, no repel zone,
// no swirl. They drift toward the cursor like moths to a porch light, then wander off.
const MOUSE_RADIUS = 260
const MOUSE_RADIUS_SQ = MOUSE_RADIUS * MOUSE_RADIUS
const MOUSE_ATTRACT = 0.012
const VELOCITY_DAMPING = 0.978   // slightly more drag than aurora — reads "lazier"
const MIN_SPEED = 0.025
const MAX_SPEED = 0.9
const SEED_MAX_SPEED = 0.06
// Curl-noise approximation: sample two scalar fields, take perpendicular gradients.
// This is divergence-reduced (vs. aurora's gradient field) so fireflies don't pile
// up in agreement regions — they slowly meander instead.
const FLOW_AMP = 0.004
const FLOW_SCALE_XY = 0.0022
const FLOW_SCALE_T = 0.04

interface Firefly {
  x: number
  y: number
  vx: number
  vy: number
  r: number
  hue: number              // warm range: amber/yellow/yellow-green
  pulseRate: number        // per-particle Hz (0.4–1.0)
  pulsePhase: number       // 0..2π
  blinkAt: number          // next epoch (sec) for an extra-bright spike
}

const flies: Firefly[] = []
let viewW = 0
let viewH = 0
let dpr = 1

function targetCount(w: number, h: number): number {
  // Fewer than aurora — fireflies should feel sparse.
  return Math.max(14, Math.min(45, Math.round((w * h) / 38000)))
}

function makeFirefly(w: number, h: number, time: number): Firefly {
  const a = Math.random() * Math.PI * 2
  const s = MIN_SPEED + Math.random() * (SEED_MAX_SPEED - MIN_SPEED)
  return {
    x: Math.random() * w,
    y: Math.random() * h,
    vx: Math.cos(a) * s,
    vy: Math.sin(a) * s,
    r: 1.4 + Math.random() * 1.6,
    hue: 40 + Math.random() * 50,                       // 40 (amber) → 90 (yellow-green)
    pulseRate: 0.4 + Math.random() * 0.6,
    pulsePhase: Math.random() * Math.PI * 2,
    blinkAt: time + 4 + Math.random() * 12,
  }
}

function reconcile(w: number, h: number, time: number) {
  const want = targetCount(w, h)
  for (const f of flies) {
    if (f.x < 0 || f.x > w) f.x = Math.random() * w
    if (f.y < 0 || f.y > h) f.y = Math.random() * h
  }
  while (flies.length < want) flies.push(makeFirefly(w, h, time))
  if (flies.length > want) flies.length = want
}

let bgGrad: CanvasGradient | null = null
let bgGradFrame = 0
const BG_REBUILD_EVERY = 32  // bg shifts very slowly — no need to rebuild often

function buildBgGrad(ctx: CanvasRenderingContext2D, w: number, h: number, time: number) {
  // Forest-at-night palette: deep teal → near-black navy. Subtle warm wash near top.
  const t1 = (Math.sin(time * 0.05) + 1) / 2
  const g = ctx.createLinearGradient(0, 0, 0, h)
  g.addColorStop(0, `hsl(${200 + t1 * 10}, 55%, ${4 + t1 * 2}%)`)
  g.addColorStop(0.6, `hsl(${175 + t1 * 8}, 45%, ${5 + t1 * 2}%)`)
  g.addColorStop(1, 'hsl(220, 50%, 2%)')
  return g
}

function animate() {
  const c = canvas.value
  if (!c) return
  const ctx = c.getContext('2d')
  if (!ctx) return

  if (document.hidden) { raf = requestAnimationFrame(animate); return }

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

  if (mouseX < -9000) { smoothMouseX = mouseX; smoothMouseY = mouseY }
  else if (smoothMouseX < -9000) { smoothMouseX = mouseX; smoothMouseY = mouseY }
  else {
    smoothMouseX += (mouseX - smoothMouseX) * MOUSE_FOLLOW
    smoothMouseY += (mouseY - smoothMouseY) * MOUSE_FOLLOW
  }

  for (const f of flies) {
    // Curl-flavored drift: two orthogonal scalar samples, used as -∂/∂x, ∂/∂y.
    // Cheaper than real curl noise but visibly less prone to clumping than the
    // aurora's gradient field.
    const n1 = Math.sin(f.x * FLOW_SCALE_XY + time * FLOW_SCALE_T)
    const n2 = Math.cos(f.y * FLOW_SCALE_XY + time * FLOW_SCALE_T * 0.83)
    f.vx += n2 * FLOW_AMP
    f.vy += -n1 * FLOW_AMP

    // Pure attract — no repel, no swirl. Fireflies slowly home in if cursor lingers.
    const mdx = smoothMouseX - f.x
    const mdy = smoothMouseY - f.y
    const distSq = mdx * mdx + mdy * mdy
    if (distSq < MOUSE_RADIUS_SQ && distSq > 0.0001) {
      const dist = Math.sqrt(distSq)
      const t = 1 - dist / MOUSE_RADIUS
      const edge = t * t * (3 - 2 * t)
      f.vx += (mdx / dist) * edge * MOUSE_ATTRACT
      f.vy += (mdy / dist) * edge * MOUSE_ATTRACT
    }

    f.vx *= VELOCITY_DAMPING
    f.vy *= VELOCITY_DAMPING
    const sp = Math.sqrt(f.vx * f.vx + f.vy * f.vy)
    if (sp > MAX_SPEED) {
      const k = MAX_SPEED / sp
      f.vx *= k; f.vy *= k
    } else if (sp < MIN_SPEED && sp > 0) {
      const a = Math.atan2(f.vy, f.vx)
      f.vx = Math.cos(a) * MIN_SPEED
      f.vy = Math.sin(a) * MIN_SPEED
    }

    f.x += f.vx
    f.y += f.vy
    if (f.x < -10) f.x = w + 10
    if (f.x > w + 10) f.x = -10
    if (f.y < -10) f.y = h + 10
    if (f.y > h + 10) f.y = -10

    // Pulse — slow per-particle sine, [0.25..1] alpha multiplier.
    let pulse = 0.25 + 0.75 * (0.5 + 0.5 * Math.sin(time * f.pulseRate * Math.PI * 2 + f.pulsePhase))

    // Occasional blink: spike to ~1.4× for ~0.4s, then schedule next.
    if (time > f.blinkAt) {
      const into = time - f.blinkAt
      if (into < 0.4) {
        const blinkCurve = Math.sin((into / 0.4) * Math.PI)  // 0→1→0
        pulse = Math.min(1, pulse + blinkCurve * 0.5)
      } else {
        f.blinkAt = time + 6 + Math.random() * 14
      }
    }

    // Big soft halo first (additive look via low-alpha fill)
    ctx.fillStyle = `hsla(${f.hue}, 95%, 70%, ${0.10 * pulse})`
    ctx.beginPath()
    ctx.arc(f.x, f.y, f.r * 7, 0, Math.PI * 2)
    ctx.fill()

    // Mid glow
    ctx.fillStyle = `hsla(${f.hue}, 95%, 75%, ${0.22 * pulse})`
    ctx.beginPath()
    ctx.arc(f.x, f.y, f.r * 3, 0, Math.PI * 2)
    ctx.fill()

    // Bright core
    ctx.fillStyle = `hsla(${f.hue}, 100%, 88%, ${0.95 * pulse})`
    ctx.beginPath()
    ctx.arc(f.x, f.y, f.r, 0, Math.PI * 2)
    ctx.fill()
  }

  raf = requestAnimationFrame(animate)
}

let resizeRaf = 0
function scheduleResize() {
  if (resizeRaf) return
  resizeRaf = requestAnimationFrame(() => { resizeRaf = 0; resize() })
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
  reconcile(viewW, viewH, performance.now() / 1000)
  bgGrad = null
}

function onMouseMove(e: MouseEvent) { mouseX = e.clientX; mouseY = e.clientY }
function onMouseLeave() { mouseX = -9999; mouseY = -9999 }

function paintStatic() {
  const c = canvas.value
  if (!c) return
  const ctx = c.getContext('2d')
  if (!ctx) return
  const g = ctx.createLinearGradient(0, 0, 0, viewH)
  g.addColorStop(0, 'hsl(200, 55%, 5%)')
  g.addColorStop(1, 'hsl(220, 50%, 2%)')
  ctx.fillStyle = g
  ctx.fillRect(0, 0, viewW, viewH)
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

  if (reduceMotion) paintStatic()
  else raf = requestAnimationFrame(animate)
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
  <canvas ref="canvas" class="fireflies-canvas" />
</template>

<style scoped>
.fireflies-canvas {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  display: block;
}
</style>
