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
        accent: {
          pink: '#FE7DA8',
          yellow: '#FFD440',
          cyan: '#27CCF3',
          green: '#A9D877',
          orange: '#F8A16F',
        },
        status: {
          success: '#A9D877',
          error: '#DC2626',
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
