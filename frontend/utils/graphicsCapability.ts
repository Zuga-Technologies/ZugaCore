/**
 * graphicsCapability — detect whether the browser actually has GPU/hardware
 * acceleration, so the app can degrade gracefully when it doesn't.
 *
 * Why this exists: the app shell (TopNav/AppShell glass) uses `backdrop-filter:
 * blur()` layered OVER an animated wallpaper (looping <video> or an RAF particle
 * canvas). With hardware acceleration ON the GPU composites that for free; with
 * it OFF the CPU re-rasterizes a full-screen Gaussian blur every frame, and since
 * the blurred chrome wraps every studio, the WHOLE app crawls. The fix is to
 * detect the no-accel case and drop the expensive effects (static wallpaper +
 * opaque chrome) — the blur is cosmetic, the UI is fully legible without it.
 *
 * Detection signal: when Chrome/Edge run with hardware acceleration disabled (or
 * the GPU is blocklisted), WebGL falls back to a SOFTWARE rasterizer — SwiftShader
 * (Chrome), llvmpipe (Mesa), or "Microsoft Basic Render". Reading the unmasked
 * renderer string is the most reliable cross-machine tell. If WebGL can't be
 * created at all, that's also no-accel. prefers-reduced-motion and very low-core
 * devices are treated as lite too.
 */

export interface GraphicsCapability {
  accelerated: boolean
  /** true when heavy effects (animated wallpaper, backdrop blur) should be dropped */
  lite: boolean
  renderer: string
  reason: string
}

const SOFTWARE_RENDERER = /swiftshader|llvmpipe|software|basic\s*render|microsoft\s*basic|softpipe|mesa\s+offscreen/i

// User override (Settings → Appearance → "Force full effects"). When set, the
// animated wallpaper + backdrop blur stay ON regardless of detection — for the
// rare user with a capable GPU who manually disabled browser acceleration.
const FORCE_FULL_KEY = 'zugaapp:force-full-effects'
/** Fired when the override flips so live components (BackgroundTheme) can react
 *  without a reload. */
export const GPU_OVERRIDE_EVENT = 'zuga:gpu-effects-override'

export function getForceFullEffects(): boolean {
  try {
    return localStorage.getItem(FORCE_FULL_KEY) === '1'
  } catch {
    return false
  }
}

let cached: GraphicsCapability | null = null

/** Read the unmasked GPU renderer string, or '' if WebGL is unavailable. */
function readRenderer(): { renderer: string; hadContext: boolean } {
  try {
    const canvas = document.createElement('canvas')
    const gl = (canvas.getContext('webgl') ||
      canvas.getContext('experimental-webgl')) as WebGLRenderingContext | null
    if (!gl) return { renderer: '', hadContext: false }
    const dbg = gl.getExtension('WEBGL_debug_renderer_info')
    const renderer = dbg
      ? String(gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) || '')
      : String(gl.getParameter(gl.RENDERER) || '')
    // Release the context promptly so the probe leaves no live GL context behind.
    gl.getExtension('WEBGL_lose_context')?.loseContext()
    return { renderer, hadContext: true }
  } catch {
    return { renderer: '', hadContext: false }
  }
}

/** Detect once and cache. Never throws — defaults to "accelerated" on any error
 *  so we don't degrade a capable machine because a probe misbehaved. */
export function detectGraphicsCapability(): GraphicsCapability {
  if (cached) return cached

  let renderer = ''
  let accelerated = true
  let reason = 'accelerated'

  try {
    const probe = readRenderer()
    renderer = probe.renderer
    if (!probe.hadContext) {
      accelerated = false
      reason = 'no-webgl-context'
    } else if (SOFTWARE_RENDERER.test(renderer)) {
      accelerated = false
      reason = `software-renderer:${renderer}`
    }
  } catch {
    // leave accelerated=true — fail open
  }

  const reducedMotion =
    typeof window !== 'undefined' &&
    !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

  // Very low-core devices choke on the animated-backdrop combo even with a GPU.
  // Conservative: only flip on <=2 logical cores (rare on real desktops).
  const lowCore =
    typeof navigator !== 'undefined' &&
    typeof navigator.hardwareConcurrency === 'number' &&
    navigator.hardwareConcurrency > 0 &&
    navigator.hardwareConcurrency <= 2

  let lite = !accelerated || reducedMotion || lowCore
  if (lite && reason === 'accelerated') {
    reason = reducedMotion ? 'prefers-reduced-motion' : 'low-core'
  }

  // User override wins: force full effects on regardless of what we detected.
  if (lite && getForceFullEffects()) {
    lite = false
    reason = `forced-full-effects(was:${reason})`
  }

  cached = { accelerated, lite, renderer, reason }
  return cached
}

/** Run detection and tag <html> with `gpu-lite` when heavy effects should drop.
 *  Call once at app boot, before mount. Idempotent and safe to call anywhere. */
export function applyGraphicsClass(): GraphicsCapability {
  const cap = detectGraphicsCapability()
  try {
    if (cap.lite && typeof document !== 'undefined') {
      document.documentElement.classList.add('gpu-lite')
    }
  } catch {
    /* no-op */
  }
  return cap
}

/** Set the "force full effects" override, re-detect, re-apply the <html> class
 *  live, and notify listeners — so the change takes effect without a reload. */
export function setForceFullEffects(on: boolean): void {
  try {
    if (on) localStorage.setItem(FORCE_FULL_KEY, '1')
    else localStorage.removeItem(FORCE_FULL_KEY)
  } catch {
    /* storage disabled — override won't persist, but still apply for this session */
  }
  cached = null
  const cap = detectGraphicsCapability()
  try {
    document.documentElement.classList.toggle('gpu-lite', cap.lite)
    window.dispatchEvent(new CustomEvent(GPU_OVERRIDE_EVENT, { detail: cap }))
  } catch {
    /* no-op */
  }
}

/** Reactive-free getter for components that want to branch in JS. */
export function isGpuLite(): boolean {
  return detectGraphicsCapability().lite
}

/** Test-only: clear the memoized result. */
export function __resetGraphicsCapabilityCache(): void {
  cached = null
}
