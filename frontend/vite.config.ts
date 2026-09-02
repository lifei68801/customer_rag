import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  // 前端此前没有测试框架。这次引入 vitest 是因为侧边栏重组要改的东西
  // ——路由映射、旧路径重定向、分组展开逻辑、待办徽标——全是**可断言的
  // 行为**而不是视觉效果，正好是测试能覆盖的部分。视觉仍然靠人眼。
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.test.{ts,tsx}'],
    setupFiles: ['src/test-setup.ts'],
    // 默认 5s 在全量并发下不够：22 个文件同时跑时，懒加载 chunk 和多次
    // 重渲染会把单条测试推过 5s，表现是"单独跑绿、全量跑红"这种最难查的
    // 假红。asyncUtilTimeout 提到 5s（见 test-setup.ts），这里必须比它宽，
    // 否则先撞的是 testTimeout，报出来的错跟真正的原因无关。
    testTimeout: 15000,
  },
  server: {
    proxy: {
      '/agent': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
      '/api': 'http://localhost:8000',
    },
  },
})
