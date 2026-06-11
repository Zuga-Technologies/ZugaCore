/**
 * Background theme definitions — video-first with canvas fallback.
 *
 * Video files go in public/backgrounds/ as MP4.
 * Themes persist in localStorage under 'zugalife-bg-theme'.
 */
import { apiUrl } from '../api/client'

export type ThemeId =
  | 'none'
  | 'custom'
  | 'aurora-particles'
  | 'fireflies'
  | 'lattice'
  | 'ai-particles'

export interface ThemeDefinition {
  id: ThemeId
  name: string
  description: string
  preview: string
  /**
   * Built-in interactive scene id. When set, BackgroundTheme.vue lazy-loads the
   * matching Vue component from ./scenes/ and renders it as the wallpaper.
   * Used for Wallpaper-Engine-style live scenes (particles, shaders, audio-reactive).
   */
  scene?: string
  overlay?: number      // dark overlay opacity (0-1) for bright scenes
  fallbackBg?: string   // CSS gradient fallback while scene mounts
}

export const THEMES: ThemeDefinition[] = [
  {
    id: 'none',
    name: 'Default Dark',
    description: 'Clean dark background',
    preview: 'linear-gradient(135deg, #0a0a0a, #1a1a1a)',
  },
  {
    id: 'aurora-particles',
    name: 'Aurora Particles',
    description: 'Live animated aurora with mouse-reactive particle field',
    preview: 'linear-gradient(135deg, #1a0a2e, #2a1a5e, #0ea5e9)',
    scene: 'aurora-particles',
    fallbackBg: 'linear-gradient(135deg, #0a0a1a, #1a0a2e)',
  },
  {
    id: 'fireflies',
    name: 'Fireflies',
    description: 'Warm pulsing fireflies drifting through forest dusk',
    preview: 'linear-gradient(135deg, #0b1d1d, #112024, #2d2a14)',
    scene: 'fireflies',
    fallbackBg: 'linear-gradient(180deg, #0b1d1d, #050a14)',
  },
  {
    id: 'lattice',
    name: 'Lattice',
    description: 'Cool geometric grid that ripples under your cursor',
    preview: 'linear-gradient(135deg, #061827, #0a2a3e, #061224)',
    scene: 'lattice',
    fallbackBg: 'radial-gradient(circle at 50% 50%, #0a2a3e, #050a14)',
  },
  {
    id: 'ai-particles',
    name: 'AI Particles',
    description: 'Describe a vibe — AI tunes the particle field',
    preview: 'linear-gradient(135deg, #2a0f4a, #6e2db5, #f43f9d)',
    scene: 'ai-particles',
    fallbackBg: 'linear-gradient(135deg, #1a0a2e, #2a0f4a)',
  },
  {
    id: 'custom',
    name: 'Custom',
    description: 'Upload your own image as a wallpaper',
    preview: 'linear-gradient(135deg, #333, #555, #333)',
  },
]

// --- AI Particle wallpaper config ---
// Schema mirrors backend ParticleConfig in ZugaApp/backend/core/wallpaper/schemas.py.
// Stored client-side because it's a small render-config blob, not an asset.

export type ParticleShape = 'circle' | 'square' | 'streak' | 'sparkle' | 'cross'
export type GradientMode = 'linear-diagonal' | 'linear-vertical' | 'radial' | 'conic'
export type CursorMode = 'auto' | 'attract' | 'repel' | 'swirl' | 'explode' | 'none'

export interface AIParticleNebula {
  count: number      // 0-3
  hueBase: number    // 0-360
  intensity: number  // 0-1
  driftSpeed: number // 0-1
}

export interface AIParticleConfig {
  name: string
  background: {
    colors: string[]
    mode?: GradientMode
  }
  particles: {
    count: number
    hueBase: number
    hueRange: number
    saturation: number
    lightness: number
    sizeMin: number
    sizeMax: number
    glow: number
    speed: number
    mouseRadius: number
    mouseAttract: number
    // Tier 1 additions — optional in stored configs to keep older payloads valid.
    shape?: ParticleShape
    trail?: number
    windX?: number
    windY?: number
    // Tier 3 additions — also optional.
    connectDistance?: number
    cursorMode?: CursorMode
  }
  flow: { amp: number; swirl: number }
  // Tier 2 additions — optional.
  nebula?: AIParticleNebula
  vignette?: number
}

const AI_PARTICLE_CONFIG_KEY = 'zugalife-bg-ai-particle-config'
const AI_PARTICLE_PROMPT_KEY = 'zugalife-bg-ai-particle-prompt'

export function getAIParticleConfig(): AIParticleConfig | null {
  const raw = localStorage.getItem(AI_PARTICLE_CONFIG_KEY)
  if (!raw) return null
  try { return JSON.parse(raw) as AIParticleConfig } catch { return null }
}
export function saveAIParticleConfig(cfg: AIParticleConfig) {
  localStorage.setItem(AI_PARTICLE_CONFIG_KEY, JSON.stringify(cfg))
}
export function getAIParticlePrompt(): string {
  return localStorage.getItem(AI_PARTICLE_PROMPT_KEY) || ''
}
export function saveAIParticlePrompt(prompt: string) {
  localStorage.setItem(AI_PARTICLE_PROMPT_KEY, prompt)
}

// --- AI Ambient helpers ---
// Note: the 'theme' values stored under AI_AMBIENT_KEY are NOT ThemeId values.
// They are short theme-prompt names accepted by ZugaImage's AMBIENT_THEMES dict
// (see ZugaImage/backend/routes.py:659). Default 'cyberpunk' is a valid AMBIENT_THEMES
// key — NOT to be confused with the 'cyberpunk-city' ThemeId enum entry above.
// Valid keys at time of writing: cyberpunk, fantasy, nature, space, abstract.

const AI_AMBIENT_KEY = 'zugalife-bg-ai-theme'
const AI_AMBIENT_INTERVAL_KEY = 'zugalife-bg-ai-interval'

export function getAIAmbientTheme(): string {
  return localStorage.getItem(AI_AMBIENT_KEY) || 'cyberpunk'
}
export function saveAIAmbientTheme(theme: string) {
  localStorage.setItem(AI_AMBIENT_KEY, theme)
}
export function getAIAmbientInterval(): number {
  const val = localStorage.getItem(AI_AMBIENT_INTERVAL_KEY)
  return val ? parseInt(val) : 30
}
export function saveAIAmbientInterval(minutes: number) {
  localStorage.setItem(AI_AMBIENT_INTERVAL_KEY, String(minutes))
}

// --- Storage helpers ---

const STORAGE_KEY = 'zugalife-bg-theme'
const CUSTOM_IMG_KEY = 'zugalife-bg-custom-img'
const CUSTOM_OPACITY_KEY = 'zugalife-bg-custom-opacity'

export function getSavedTheme(): ThemeId | string {
  const saved = localStorage.getItem(STORAGE_KEY)
  if (!saved) return 'none'
  // User-theme ids: pass through; renderer's fetch will validate
  if (saved.startsWith('th_')) return saved
  // Built-in ids: must be in the registry
  if (THEMES.some(t => t.id === saved)) return saved as ThemeId
  return 'none'
}
export function saveTheme(id: ThemeId | string) {
  localStorage.setItem(STORAGE_KEY, id)
  // Account-scoped: persist server-side so the theme follows the user
  // across devices. Fire-and-forget — silent on network/auth failure
  // so anonymous + offline paths keep working off localStorage.
  try {
    fetch(apiUrl('/api/auth/bg-theme-pref'), {
      method: 'PATCH',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ theme: id }),
    }).catch(() => {})
  } catch { /* noop */ }
}

// Hydrate from server when the page loads (post-auth). Caller awaits, then
// reads getSavedTheme(). Silent on failure so anon visitors still render.
export async function hydrateThemeFromServer(): Promise<void> {
  try {
    const res = await fetch(apiUrl('/api/auth/bg-theme-pref'), { credentials: 'include' })
    if (!res.ok) return
    const data = await res.json()
    const theme = (data && typeof data.theme === 'string') ? data.theme : null
    if (theme) localStorage.setItem(STORAGE_KEY, theme)
  } catch { /* noop */ }
}
export function getTheme(id: ThemeId): ThemeDefinition {
  return THEMES.find(t => t.id === id) || THEMES[0]
}

export function getCustomImage(): string | null { return localStorage.getItem(CUSTOM_IMG_KEY) }
export function saveCustomImage(dataUrl: string) { localStorage.setItem(CUSTOM_IMG_KEY, dataUrl) }
export function removeCustomImage() { localStorage.removeItem(CUSTOM_IMG_KEY) }
export function getCustomOpacity(): number {
  const val = localStorage.getItem(CUSTOM_OPACITY_KEY)
  return val ? parseFloat(val) : 0.3
}
export function saveCustomOpacity(opacity: number) { localStorage.setItem(CUSTOM_OPACITY_KEY, String(opacity)) }

// --- Custom video (IndexedDB — too large for localStorage) ---

const CUSTOM_VIDEO_SPEED_KEY = 'zugalife-bg-custom-video-speed'
const IDB_NAME = 'zugalife-bg'
const IDB_STORE = 'custom-media'
const IDB_VIDEO_KEY = 'custom-video'

function openIDB(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(IDB_NAME, 1)
    req.onupgradeneeded = () => { req.result.createObjectStore(IDB_STORE) }
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error)
  })
}

export async function saveCustomVideo(blob: Blob): Promise<void> {
  const db = await openIDB()
  return new Promise((resolve, reject) => {
    const tx = db.transaction(IDB_STORE, 'readwrite')
    tx.objectStore(IDB_STORE).put(blob, IDB_VIDEO_KEY)
    tx.oncomplete = () => { db.close(); resolve() }
    tx.onerror = () => { db.close(); reject(tx.error) }
  })
}

export async function getCustomVideo(): Promise<string | null> {
  try {
    const db = await openIDB()
    return new Promise((resolve) => {
      const tx = db.transaction(IDB_STORE, 'readonly')
      const req = tx.objectStore(IDB_STORE).get(IDB_VIDEO_KEY)
      req.onsuccess = () => {
        db.close()
        if (req.result instanceof Blob) {
          resolve(URL.createObjectURL(req.result))
        } else {
          resolve(null)
        }
      }
      req.onerror = () => { db.close(); resolve(null) }
    })
  } catch { return null }
}

export async function removeCustomVideo(): Promise<void> {
  try {
    const db = await openIDB()
    return new Promise((resolve) => {
      const tx = db.transaction(IDB_STORE, 'readwrite')
      tx.objectStore(IDB_STORE).delete(IDB_VIDEO_KEY)
      tx.oncomplete = () => { db.close(); resolve() }
      tx.onerror = () => { db.close(); resolve() }
    })
  } catch { /* ignore */ }
}

export function getCustomVideoSpeed(): number {
  const val = localStorage.getItem(CUSTOM_VIDEO_SPEED_KEY)
  return val ? parseFloat(val) : 0.5
}
export function saveCustomVideoSpeed(speed: number) {
  localStorage.setItem(CUSTOM_VIDEO_SPEED_KEY, String(speed))
}

/** Check what type of custom media is stored */
export function getCustomMediaType(): 'image' | 'video' | null {
  if (localStorage.getItem(CUSTOM_IMG_KEY)) return 'image'
  // Video check is async, so we use a sync flag
  return localStorage.getItem('zugalife-bg-has-custom-video') === '1' ? 'video' : null
}
export function setCustomVideoFlag(has: boolean) {
  if (has) localStorage.setItem('zugalife-bg-has-custom-video', '1')
  else localStorage.removeItem('zugalife-bg-has-custom-video')
}
