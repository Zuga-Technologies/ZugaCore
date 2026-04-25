// @core/wallpaper — shared wallpaper module
//
// Owns: ThemeDefinition type, THEMES registry, BackgroundTheme renderer,
// localStorage/IndexedDB storage helpers.
//
// Consumers: ZugaLife (re-exports), ZugaApp App.vue (via @studios/life shim),
// future: Zugabot, ZugaImage/ZugaVideo generator hooks, Wallpaper Engine export.

export * from './registry'
export { default as BackgroundTheme } from './BackgroundTheme.vue'
