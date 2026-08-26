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
          primary: 'rgb(var(--color-accent-primary) / <alpha-value>)',
          secondary: 'rgb(var(--color-accent-secondary) / <alpha-value>)',
          pink: 'rgb(var(--color-accent-pink) / <alpha-value>)',
          yellow: 'rgb(var(--color-accent-yellow) / <alpha-value>)',
          cyan: 'rgb(var(--color-accent-cyan) / <alpha-value>)',
          green: 'rgb(var(--color-accent-green) / <alpha-value>)',
          orange: 'rgb(var(--color-accent-orange) / <alpha-value>)',
        },
        status: {
          success: 'rgb(var(--color-status-success) / <alpha-value>)',
          error: 'rgb(var(--color-status-error) / <alpha-value>)',
          'error-strong': 'rgb(var(--color-status-error-strong) / <alpha-value>)',
          'error-hover': 'rgb(var(--color-status-error-hover) / <alpha-value>)',
        },
        'on-accent': 'rgb(var(--color-text-on-accent) / <alpha-value>)',
      },
      borderColor: {
        subtle: 'var(--color-border-subtle)',
      },
      fontFamily: {
        sans: [
          '"IBM Plex Sans"',
          'system-ui',
          '"PingFang SC"',
          '"Microsoft YaHei"',
          'sans-serif',
        ],
        mono: ['"IBM Plex Mono"', 'ui-monospace', '"SFMono-Regular"', 'monospace'],
      },
      borderRadius: {
        chip: '2px',
        control: '2px',
        DEFAULT: '2px',
        card: '3px',
        panel: '3px',
        modal: '4px',
        container: '4px',
      },
    },
  },
  plugins: [],
} satisfies Config
