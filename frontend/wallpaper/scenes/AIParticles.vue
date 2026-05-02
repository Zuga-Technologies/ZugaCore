<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { getAIParticleConfig, type AIParticleConfig } from '../registry'

const canvas = ref<HTMLCanvasElement | null>(null)
let raf: number | null = null

let mouseX = -9999
let mouseY = -9999
let smoothMouseX = -9999
let smoothMouseY = -9999
const MOUSE_FOLLOW = 0.18

const DEFAULT_CONFIG: AIParticleConfig = {
  name: 'Default',
  background: { colors: ['#0a0a1a', '#1a0a2e', '#0ea5e9'] },
  particles: {
    count: 60, hueBase: 220, hueRange: 80, saturation: 70, lightness: 65,
    sizeMin: 0.8, sizeMax: 2.5, glow: 0.5, speed: 1.0,
    mouseRadius: 220, mouseAttract: 0.5,
  },
  flow: { amp: 0.006, swirl: 0.7 },
}

let cfg: AIParticleConfig = getAIParticleConfig() || DEFAULT_CONFIG

interface Particle { x: number; y: number; vx: number; vy: number; r: number; hue: number; alpha: number }

const particles: Particle[] = []
let viewW = 0
let viewH = 0
let dpr = 1

const VELOCITY_DAMPING = 0.985
const MIN_SPEED = 0.04
const SEED_MAX_SPEED = 0.10
const FLOW_SCALE_XY = 0.0018
const FLOW_SCALE_T = 0.06
const MOUSE_REPEL_PX = 50

// Derived per-config — recomputed on hot-reload.
let MOUSE_RADIUS_SQ = 0
let MAX_SPEED = 1.4
let MOUSE_ATTRACT_SCALED = 0.022
let MOUSE_RADIAL = 0.3
let MOUSE_TANGENTIAL = 0.7

function recomputeDerived() {
  MOUSE_RADIUS_SQ = cfg.particles.mouseRadius * cfg.particles.mouseRadius
  MAX_SPEED = 0.6 + cfg.particles.speed * 0.8        // speed=1 → 1.4 (matches Aurora)
  MOUSE_ATTRACT_SCALED = 0.044 * cfg.particles.mouseAttract
  MOUSE_TANGENTIAL = cfg.flow.swirl
  MOUSE_RADIAL = 1 - cfg.flow.swirl
}
recomputeDerived()

function makeParticle(w: number, h: number): Particle {
  const a = Math.random() * Math.PI * 2
  const s = MIN_SPEED + Math.random() * (SEED_MAX_SPEED - MIN_SPEED)
  const sizeMin = cfg.particles.sizeMin
  const sizeMax = Math.max(sizeMin, cfg.particles.sizeMax)
  return {
    x: Math.random() * w,
    y: Math.random() * h,
    vx: Math.cos(a) * s,
    vy: Math.sin(a) * s,
    r: sizeMin + Math.random() * (sizeMax - sizeMin),
    hue: cfg.particles.hueBase + (Math.random() - 0.5) * cfg.particles.hueRange,
    alpha: Math.random() * 0.5 + 0.3,
  }
}

function reconcileParticles(w: number, h: number) {
  const want = cfg.particles.count
  for (const p of particles) {
    if (p.x < 0 || p.x > w) p.x = Math.random() * w
    if (p.y < 0 || p.y > h) p.y = Math.random() * h
  }
  while (particles.length < want) particles.push(makeParticle(w, h))
  if (particles.length > want) particles.length = want
}

let bgGrad: CanvasGradient | null = null
function buildBgGrad(ctx: CanvasRenderingContext2D, w: number, h: number) {
  const colors = cfg.background.colors.length > 0 ? cfg.background.colors : DEFAULT_CONFIG.background.colors
  const g = ctx.createLinearGradient(0, 0, w, h)
  if (colors.length === 1) {
    g.addColorStop(0, colors[0])
    g.addColorStop(1, colors[0])
  } else {
    colors.forEach((c, i) => g.addColorStop(i / (colors.length - 1), c))
  }
  return g
}

function animate() {
  const c = canvas.value
  if (!c) return
  const ctx = c.getContext('2d')
  if (!ctx) return

  if (document.hidden) {
    raf = requestAnimationFrame(animate)
    return
  }

  const w = viewW
  const h = viewH
  const time = performance.now() / 1000

  if (!bgGrad) bgGrad = buildBgGrad(ctx, w, h)
  ctx.fillStyle = bgGrad
  ctx.fillRect(0, 0, w, h)

  if (mouseX < -9000) {
    smoothMouseX = mouseX
    smoothMouseY = mouseY
  } else if (smoothMouseX < -9000) {
    smoothMouseX = mouseX
    smoothMouseY = mouseY
  } else {
    smoothMouseX += (mouseX - smoothMouseX) * MOUSE_FOLLOW
    smoothMouseY += (mouseY - smoothMouseY) * MOUSE_FOLLOW
  }

  const flowAmp = cfg.flow.amp
  const speedMul = cfg.particles.speed
  const sat = cfg.particles.saturation
  const light = cfg.particles.lightness
  const glow = cfg.particles.glow
  const timeTwinkle = time * 0.9

  for (const p of particles) {
    const ang = (Math.sin(p.x * FLOW_SCALE_XY + time * FLOW_SCALE_T)
               + Math.cos(p.y * FLOW_SCALE_XY + time * FLOW_SCALE_T * 0.83)) * Math.PI
    p.vx += Math.cos(ang) * flowAmp
    p.vy += Math.sin(ang) * flowAmp

    const mdx = smoothMouseX - p.x
    const mdy = smoothMouseY - p.y
    const distSq = mdx * mdx + mdy * mdy
    if (distSq < MOUSE_RADIUS_SQ && distSq > 0.0001) {
      const dist = Math.sqrt(distSq)
      const t = 1 - dist / cfg.particles.mouseRadius
      const edge = t * t * (3 - 2 * t)
      const rx = mdx / dist
      const ry = mdy / dist
      const tx = -ry
      const ty = rx
      const sign = dist < MOUSE_REPEL_PX ? -1 : 1
      p.vx += (sign * rx * MOUSE_RADIAL + tx * MOUSE_TANGENTIAL) * edge * MOUSE_ATTRACT_SCALED
      p.vy += (sign * ry * MOUSE_RADIAL + ty * MOUSE_TANGENTIAL) * edge * MOUSE_ATTRACT_SCALED
    }

    p.vx *= VELOCITY_DAMPING
    p.vy *= VELOCITY_DAMPING
    const sp = Math.sqrt(p.vx * p.vx + p.vy * p.vy)
    if (sp > MAX_SPEED) {
      const k = MAX_SPEED / sp
      p.vx *= k
      p.vy *= k
    } else if (sp < MIN_SPEED && sp > 0) {
      const a = Math.atan2(p.vy, p.vx)
      p.vx = Math.cos(a) * MIN_SPEED
      p.vy = Math.sin(a) * MIN_SPEED
    }

    p.x += p.vx * speedMul
    p.y += p.vy * speedMul
    if (p.x < -5) p.x = w + 5
    if (p.x > w + 5) p.x = -5
    if (p.y < -5) p.y = h + 5
    if (p.y > h + 5) p.y = -5

    const twinkle = (Math.sin(timeTwinkle + p.hue * 0.05) + 1) / 2
    const a = p.alpha * (0.5 + twinkle * 0.5)
    ctx.fillStyle = `hsla(${p.hue}, ${sat}%, ${light}%, ${a})`
    ctx.beginPath()
    ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2)
    ctx.fill()

    if (glow > 0) {
      ctx.fillStyle = `hsla(${p.hue}, ${sat}%, ${light}%, ${p.alpha * 0.18 * glow})`
      ctx.beginPath()
      ctx.arc(p.x, p.y, p.r * (3 + glow * 3), 0, Math.PI * 2)
      ctx.fill()
    }
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
  reconcileParticles(viewW, viewH)
  bgGrad = null
}

function onMouseMove(e: MouseEvent) { mouseX = e.clientX; mouseY = e.clientY }
function onMouseLeave() { mouseX = -9999; mouseY = -9999 }

// Live-reload when config changes (other tab generated, or our own settings UI did).
function onStorage(e: StorageEvent) {
  if (e.key === 'zugalife-bg-ai-particle-config' || e.key === null) {
    cfg = getAIParticleConfig() || DEFAULT_CONFIG
    recomputeDerived()
    particles.length = 0
    reconcileParticles(viewW, viewH)
    bgGrad = null
  }
}

// Same-tab regenerate signal — `storage` only fires cross-tab.
function onConfigUpdated() {
  cfg = getAIParticleConfig() || DEFAULT_CONFIG
  recomputeDerived()
  particles.length = 0
  reconcileParticles(viewW, viewH)
  bgGrad = null
}

function paintStatic() {
  const c = canvas.value
  if (!c) return
  const ctx = c.getContext('2d')
  if (!ctx) return
  bgGrad = null
  ctx.fillStyle = buildBgGrad(ctx, viewW, viewH)
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
  window.addEventListener('storage', onStorage)
  window.addEventListener('zuga:ai-particle-config-updated', onConfigUpdated)

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
  window.removeEventListener('storage', onStorage)
  window.removeEventListener('zuga:ai-particle-config-updated', onConfigUpdated)
})
</script>

<template>
  <canvas ref="canvas" class="ai-particles-canvas" />
</template>

<style scoped>
.ai-particles-canvas {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  display: block;
}
</style>
