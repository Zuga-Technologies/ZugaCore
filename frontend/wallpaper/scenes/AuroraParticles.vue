<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'

const canvas = ref<HTMLCanvasElement | null>(null)
let raf: number | null = null
let mouseX = -9999
let mouseY = -9999
const MOUSE_RADIUS = 150
const MOUSE_FORCE = 40

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
const PARTICLE_COUNT = 80

function init(width: number, height: number) {
  particles.length = 0
  for (let i = 0; i < PARTICLE_COUNT; i++) {
    particles.push({
      x: Math.random() * width,
      y: Math.random() * height,
      vx: (Math.random() - 0.5) * 0.3,
      vy: (Math.random() - 0.5) * 0.3,
      r: Math.random() * 2 + 0.5,
      hue: 180 + Math.random() * 120,
      alpha: Math.random() * 0.5 + 0.2,
    })
  }
}

function animate() {
  const c = canvas.value
  if (!c) return
  const ctx = c.getContext('2d')
  if (!ctx) return

  const w = c.width
  const h = c.height
  const time = performance.now() / 1000

  const grad = ctx.createLinearGradient(0, 0, w, h)
  const t1 = (Math.sin(time * 0.1) + 1) / 2
  const t2 = (Math.cos(time * 0.13) + 1) / 2
  grad.addColorStop(0, `hsl(${260 + t1 * 30}, 60%, ${8 + t1 * 4}%)`)
  grad.addColorStop(0.5, `hsl(${220 + t2 * 40}, 70%, ${10 + t2 * 6}%)`)
  grad.addColorStop(1, `hsl(${190 + t1 * 30}, 80%, ${6 + t2 * 4}%)`)
  ctx.fillStyle = grad
  ctx.fillRect(0, 0, w, h)

  const auroraY = h * 0.4 + Math.sin(time * 0.3) * 30
  const auroraGrad = ctx.createLinearGradient(0, auroraY - 200, 0, auroraY + 200)
  auroraGrad.addColorStop(0, 'rgba(56, 189, 248, 0)')
  auroraGrad.addColorStop(0.5, `rgba(${100 + Math.sin(time) * 50}, ${200 + Math.cos(time * 0.7) * 30}, 255, 0.18)`)
  auroraGrad.addColorStop(1, 'rgba(167, 139, 250, 0)')
  ctx.fillStyle = auroraGrad
  ctx.fillRect(0, 0, w, h)

  for (const p of particles) {
    p.x += p.vx
    p.y += p.vy
    if (p.x < 0) p.x = w
    if (p.x > w) p.x = 0
    if (p.y < 0) p.y = h
    if (p.y > h) p.y = 0

    let drawX = p.x
    let drawY = p.y
    const dx = p.x - mouseX
    const dy = p.y - mouseY
    const dist = Math.sqrt(dx * dx + dy * dy)
    if (dist < MOUSE_RADIUS && dist > 0.01) {
      const falloff = 1 - dist / MOUSE_RADIUS
      const push = falloff * falloff * MOUSE_FORCE
      drawX = p.x + (dx / dist) * push
      drawY = p.y + (dy / dist) * push
    }

    const twinkle = (Math.sin(time * 2 + p.x * 0.01) + 1) / 2
    ctx.fillStyle = `hsla(${p.hue}, 80%, 70%, ${p.alpha * (0.5 + twinkle * 0.5)})`
    ctx.beginPath()
    ctx.arc(drawX, drawY, p.r, 0, Math.PI * 2)
    ctx.fill()

    ctx.fillStyle = `hsla(${p.hue}, 80%, 70%, ${p.alpha * 0.15})`
    ctx.beginPath()
    ctx.arc(drawX, drawY, p.r * 4, 0, Math.PI * 2)
    ctx.fill()
  }

  raf = requestAnimationFrame(animate)
}

function resize() {
  const c = canvas.value
  if (!c) return
  const dpr = Math.min(window.devicePixelRatio || 1, 2)
  c.width = window.innerWidth * dpr
  c.height = window.innerHeight * dpr
  c.style.width = window.innerWidth + 'px'
  c.style.height = window.innerHeight + 'px'
  const ctx = c.getContext('2d')
  if (ctx) ctx.scale(dpr, dpr)
  init(window.innerWidth, window.innerHeight)
}

function onMouseMove(e: MouseEvent) {
  mouseX = e.clientX
  mouseY = e.clientY
}

function onMouseLeave() {
  mouseX = -9999
  mouseY = -9999
}

onMounted(() => {
  resize()
  window.addEventListener('resize', resize)
  window.addEventListener('mousemove', onMouseMove)
  document.addEventListener('mouseleave', onMouseLeave)
  raf = requestAnimationFrame(animate)
})

onUnmounted(() => {
  if (raf) cancelAnimationFrame(raf)
  window.removeEventListener('resize', resize)
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
