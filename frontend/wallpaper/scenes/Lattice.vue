<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'

const canvas = ref<HTMLCanvasElement | null>(null)
let raf: number | null = null

let mouseX = -9999
let mouseY = -9999
let smoothMouseX = -9999
let smoothMouseY = -9999
const MOUSE_FOLLOW = 0.2

// Mouse pushes nodes outward like a hand on a sheet of cloth. No swirl, no orbit —
// nodes spring back toward their base position when the cursor leaves.
const MOUSE_RADIUS = 200
const MOUSE_RADIUS_SQ = MOUSE_RADIUS * MOUSE_RADIUS
const MOUSE_PUSH = 38              // peak displacement in px at cursor center
// Spring-back: each node has a base position; displacement decays toward it.
const SPRING_K = 0.06              // stronger = snappier return
const SPRING_DAMP = 0.82           // velocity damping for the displacement state

// Wobble: each node oscillates a little around its base independently of the cursor.
const WOBBLE_AMP = 4               // px
const WOBBLE_RATE = 0.6            // Hz baseline (jittered per node)

// Cell size — adaptive but capped. Smaller cells = denser lattice = heavier draw.
const CELL_TARGET = 84
const CELL_MIN = 70
const CELL_MAX = 110

interface Node {
  bx: number      // base x
  by: number      // base y
  x: number       // current x (= bx + dx)
  y: number       // current y (= by + dy)
  dx: number      // displacement
  dy: number
  vdx: number     // displacement velocity
  vdy: number
  phase: number   // wobble phase
  rateMul: number // wobble rate multiplier (0.7..1.3)
}

let cols = 0
let rows = 0
let cell = CELL_TARGET
const nodes: Node[] = []
let viewW = 0
let viewH = 0
let dpr = 1

function rebuildGrid(w: number, h: number) {
  // Pick cell size that divides evenly into width within [CELL_MIN, CELL_MAX].
  const idealCols = Math.round(w / CELL_TARGET)
  cell = Math.max(CELL_MIN, Math.min(CELL_MAX, w / Math.max(1, idealCols)))
  cols = Math.ceil(w / cell) + 1
  rows = Math.ceil(h / cell) + 1

  nodes.length = 0
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const bx = c * cell
      const by = r * cell
      nodes.push({
        bx, by,
        x: bx, y: by,
        dx: 0, dy: 0,
        vdx: 0, vdy: 0,
        phase: Math.random() * Math.PI * 2,
        rateMul: 0.7 + Math.random() * 0.6,
      })
    }
  }
}

let bgGrad: CanvasGradient | null = null
let bgGradFrame = 0
const BG_REBUILD_EVERY = 32

function buildBgGrad(ctx: CanvasRenderingContext2D, w: number, h: number, time: number) {
  // Cool deep palette: navy → near-black with a touch of cyan glow at center.
  const t1 = (Math.sin(time * 0.07) + 1) / 2
  const g = ctx.createRadialGradient(w / 2, h / 2, 0, w / 2, h / 2, Math.hypot(w, h) / 2)
  g.addColorStop(0, `hsl(${200 + t1 * 15}, 65%, ${7 + t1 * 3}%)`)
  g.addColorStop(0.6, 'hsl(220, 55%, 5%)')
  g.addColorStop(1, 'hsl(225, 50%, 2%)')
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

  // Update node displacement (spring-mass) + wobble.
  for (const n of nodes) {
    // Wobble — small per-node offset around the *base* position, not added to dx.
    // We sum it into x/y at the end so it doesn't fight the spring.
    const wobX = Math.cos(time * n.rateMul * WOBBLE_RATE * Math.PI * 2 + n.phase) * WOBBLE_AMP
    const wobY = Math.sin(time * n.rateMul * WOBBLE_RATE * Math.PI * 2 + n.phase * 1.3) * WOBBLE_AMP

    // Spring force toward zero displacement.
    n.vdx += -SPRING_K * n.dx
    n.vdy += -SPRING_K * n.dy

    // Mouse push (radial, outward).
    const mdx = smoothMouseX - n.bx
    const mdy = smoothMouseY - n.by
    const distSq = mdx * mdx + mdy * mdy
    if (distSq < MOUSE_RADIUS_SQ && distSq > 0.0001) {
      const dist = Math.sqrt(distSq)
      const t = 1 - dist / MOUSE_RADIUS
      const edge = t * t * (3 - 2 * t)
      // Push opposite to cursor direction (outward from cursor).
      n.vdx += -(mdx / dist) * edge * MOUSE_PUSH * 0.04
      n.vdy += -(mdy / dist) * edge * MOUSE_PUSH * 0.04
    }

    n.vdx *= SPRING_DAMP
    n.vdy *= SPRING_DAMP
    n.dx += n.vdx
    n.dy += n.vdy

    n.x = n.bx + n.dx + wobX
    n.y = n.by + n.dy + wobY
  }

  // Draw lines between right-neighbor and below-neighbor (keeps each edge once).
  // Alpha falls off with edge length deviation from cell — stretched edges read brighter.
  ctx.lineWidth = 1
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const i = r * cols + c
      const n = nodes[i]

      // Right neighbor
      if (c + 1 < cols) {
        const m = nodes[i + 1]
        const len = Math.hypot(m.x - n.x, m.y - n.y)
        const stretch = Math.min(1.6, len / cell)             // 1 = relaxed, >1 = stretched
        const alpha = Math.max(0.04, Math.min(0.55, 0.12 * stretch * stretch))
        const hue = 190 + Math.min(40, (stretch - 1) * 60)    // cyan→cyan-blue under stress
        ctx.strokeStyle = `hsla(${hue}, 80%, 65%, ${alpha})`
        ctx.beginPath()
        ctx.moveTo(n.x, n.y)
        ctx.lineTo(m.x, m.y)
        ctx.stroke()
      }
      // Bottom neighbor
      if (r + 1 < rows) {
        const m = nodes[i + cols]
        const len = Math.hypot(m.x - n.x, m.y - n.y)
        const stretch = Math.min(1.6, len / cell)
        const alpha = Math.max(0.04, Math.min(0.55, 0.12 * stretch * stretch))
        const hue = 190 + Math.min(40, (stretch - 1) * 60)
        ctx.strokeStyle = `hsla(${hue}, 80%, 65%, ${alpha})`
        ctx.beginPath()
        ctx.moveTo(n.x, n.y)
        ctx.lineTo(m.x, m.y)
        ctx.stroke()
      }
    }
  }

  // Nodes — small dots, brighter when displaced.
  for (const n of nodes) {
    const disp = Math.min(1, Math.hypot(n.dx, n.dy) / 30)
    const alpha = 0.35 + 0.55 * disp
    const r = 1.2 + disp * 1.8
    ctx.fillStyle = `hsla(${195 + disp * 25}, 90%, ${70 + disp * 15}%, ${alpha})`
    ctx.beginPath()
    ctx.arc(n.x, n.y, r, 0, Math.PI * 2)
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
  rebuildGrid(viewW, viewH)
  bgGrad = null
}

function onMouseMove(e: MouseEvent) { mouseX = e.clientX; mouseY = e.clientY }
function onMouseLeave() { mouseX = -9999; mouseY = -9999 }

function paintStatic() {
  const c = canvas.value
  if (!c) return
  const ctx = c.getContext('2d')
  if (!ctx) return
  const g = ctx.createRadialGradient(viewW / 2, viewH / 2, 0, viewW / 2, viewH / 2, Math.hypot(viewW, viewH) / 2)
  g.addColorStop(0, 'hsl(205, 65%, 9%)')
  g.addColorStop(1, 'hsl(225, 50%, 2%)')
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
  <canvas ref="canvas" class="lattice-canvas" />
</template>

<style scoped>
.lattice-canvas {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  display: block;
}
</style>
