/**
 * Background theme definitions — video-first with canvas fallback.
 *
 * Video files go in public/backgrounds/ as MP4.
 * Themes persist in localStorage under 'zugalife-bg-theme'.
 */

export type ThemeId =
  | 'none'
  | 'custom'

export interface ThemeDefinition {
  id: ThemeId
  name: string
  description: string
  preview: string
  video?: string        // path under /backgrounds/ (MP4)
  videoWebm?: string    // WebM/VP9 alternative (smaller, better decode perf)
  poster?: string       // JPEG poster frame for instant display
  overlay?: number      // dark overlay opacity (0-1) for bright videos
  fallbackBg?: string   // CSS gradient fallback while video loads
  speed?: number        // playback rate (default 0.6) — lower = slower
}

export const THEMES: ThemeDefinition[] = [
  {
    id: 'none',
    name: 'Default Dark',
    description: 'Clean dark background',
    preview: 'linear-gradient(135deg, #0a0a0a, #1a1a1a)',
  },
  {
    id: 'custom',
    name: 'Custom',
    description: 'Upload your own image as a wallpaper',
    preview: 'linear-gradient(135deg, #333, #555, #333)',
  },
]

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
export function saveTheme(id: ThemeId | string) { localStorage.setItem(STORAGE_KEY, id) }
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
