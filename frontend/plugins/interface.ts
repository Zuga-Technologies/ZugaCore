import type { RouteRecordRaw } from 'vue-router'

export interface StudioFrontendPlugin {
  /** Internal name — matches the studios/ directory name (e.g. 'life') */
  name: string
  /** Display name shown in nav and dashboard (e.g. 'Life') */
  displayName: string
  /** Short description shown on the dashboard card */
  description: string
  /** Base route path (e.g. '/life') */
  basePath: string
  /** Vue Router routes this studio registers */
  routes: RouteRecordRaw[]
  /** Navigation configuration */
  nav: {
    icon: string
    /** Sort order in the nav bar — lower numbers appear first */
    order: number
  }
  /** If true, only visible and accessible to admin users */
  adminOnly?: boolean
}
