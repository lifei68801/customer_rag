import type { Config } from 'tailwindcss'

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        paper: 'rgb(var(--color-paper) / <alpha-value>)',
        ink: {
          DEFAULT: 'rgb(var(--color-ink) / <alpha-value>)',
          soft: 'rgb(var(--color-ink-soft) / <alpha-value>)',
        },
        card: 'rgb(var(--color-card) / <alpha-value>)',
        interactive: {
          hover: 'rgb(var(--color-interactive-hover) / <alpha-value>)',
        },
        accent: {
          pink: 'rgb(var(--color-accent-pink) / <alpha-value>)',
          yellow: 'rgb(var(--color-accent-yellow) / <alpha-value>)',
          cyan: 'rgb(var(--color-accent-cyan) / <alpha-value>)',
          green: 'rgb(var(--color-accent-green) / <alpha-value>)',
          orange: 'rgb(var(--color-accent-orange) / <alpha-value>)',
        },
        status: {
          success: 'rgb(var(--color-status-success) / <alpha-value>)',
          error: 'rgb(var(--color-status-error) / <alpha-value>)',
          'error-hover': 'rgb(var(--color-status-error-hover) / <alpha-value>)',
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
