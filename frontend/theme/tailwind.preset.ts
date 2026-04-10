import type { Config } from 'tailwindcss'

/** Helper: reference a CSS custom property as RGB channels with alpha support */
const v = (name: string) => `rgb(var(--${name}) / <alpha-value>)`

export default {
  theme: {
    extend: {
      colors: {
        surface: {
          0: v('surface-0'),
          1: v('surface-1'),
          2: v('surface-2'),
          3: v('surface-3'),
          4: v('surface-4'),
        },
        accent: {
          DEFAULT: v('accent'),
          dim: v('accent-dim'),
          bright: v('accent-bright'),
        },
        'accent-alt': {
          DEFAULT: v('accent-alt'),
          dim: v('accent-alt-dim'),
          bright: v('accent-alt-bright'),
        },
        txt: {
          primary: v('txt-primary'),
          secondary: v('txt-secondary'),
          muted: v('txt-muted'),
        },
        bdr: {
          DEFAULT: v('bdr'),
          hover: v('bdr-hover'),
        },
        // Semantic gamification colors
        xp: v('color-xp'),
        prestige: v('color-prestige'),
        streak: v('color-streak'),
        success: v('color-success'),
        danger: v('color-danger'),
        info: v('color-info'),
        quest: v('color-quest'),
        challenge: v('color-challenge'),
        // Journal categories
        expressive: v('color-expressive'),
        gratitude: v('color-gratitude'),
        reflection: v('color-reflection'),
      },
      fontFamily: {
        sans: ['var(--font-sans)'],
      },
      keyframes: {
        'fade-in': {
          from: { opacity: '0' },
          to: { opacity: '1' },
        },
        'slide-up': {
          from: { opacity: '0', transform: 'translateY(6px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
        'pulse-glow': {
          '0%, 100%': { boxShadow: '0 0 8px rgb(var(--accent) / 0.3)' },
          '50%': { boxShadow: '0 0 20px rgb(var(--accent) / 0.5)' },
        },
        'dash-card-in': {
          from: { opacity: '0', transform: 'translateY(8px) scale(0.99)' },
          to: { opacity: '1', transform: 'translateY(0) scale(1)' },
        },
      },
      animation: {
        'fade-in': 'fade-in 0.15s ease-out',
        'slide-up': 'slide-up 0.25s ease-out',
        'pulse-glow': 'pulse-glow 2s ease-in-out infinite',
      },
    },
  },
} satisfies Partial<Config>
