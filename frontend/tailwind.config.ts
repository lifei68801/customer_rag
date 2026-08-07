import type { Config } from 'tailwindcss'

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        surface: {
          base: '#0A0A0F',
          raised: '#15151F',
          card: '#1B1B29',
          border: '#33333F',
        },
        accent: {
          DEFAULT: '#0EA5E9',
          soft: '#06B6D4',
        },
        content: {
          primary: '#F5F5F7',
          secondary: '#9CA3AF',
        },
        status: {
          success: '#10B981',
          error: '#EF4444',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
      },
      borderRadius: {
        card: '12px',
        bubble: '16px',
      },
    },
  },
  plugins: [],
} satisfies Config
