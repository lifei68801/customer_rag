import type { Config } from 'tailwindcss'

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        paper: 'var(--color-paper)',
        ink: {
          DEFAULT: 'var(--color-ink)',
          soft: 'var(--color-ink-soft)',
        },
        card: 'var(--color-card)',
        interactive: {
          hover: 'var(--color-interactive-hover)',
        },
        accent: {
          pink: 'var(--color-accent-pink)',
          yellow: 'var(--color-accent-yellow)',
          cyan: 'var(--color-accent-cyan)',
          green: 'var(--color-accent-green)',
          orange: 'var(--color-accent-orange)',
        },
        status: {
          success: 'var(--color-status-success)',
          error: 'var(--color-status-error)',
        },
      },
      fontFamily: {
        sans: [
          '"Space Grotesk"',
          'system-ui',
          '"PingFang SC"',
          '"Microsoft YaHei"',
          'sans-serif',
        ],
        mono: ['"Space Mono"', 'ui-monospace', '"SFMono-Regular"', 'monospace'],
      },
      boxShadow: {
        brutal: '2px 2px 0 0 var(--color-ink)',
        'brutal-sm': '1px 1px 0 0 var(--color-ink)',
      },
    },
  },
  plugins: [],
} satisfies Config
