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
          'error-hover': 'var(--color-status-error-hover)',
        },
      },
      borderColor: {
        subtle: 'var(--color-border-subtle)',
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
      borderRadius: {
        chip: '6px',
        control: '7px',
        DEFAULT: '8px',
        card: '12px',
        panel: '14px',
        modal: '18px',
        container: '24px',
      },
      boxShadow: {
        soft: '0 4px 6px -1px var(--shadow-color-1), 0 2px 4px -2px var(--shadow-color-2)',
        'soft-sm': '0 1px 3px 0 var(--shadow-color-1), 0 1px 2px -1px var(--shadow-color-2)',
      },
    },
  },
  plugins: [],
} satisfies Config
