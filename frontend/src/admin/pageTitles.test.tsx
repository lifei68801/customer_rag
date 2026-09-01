import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { NAV_GROUPS } from '../adminRoutes'

/**
 * 页面标题必须和导航里的名字一字不差。
 *
 * 点「待审关系」落到一个叫「文档抽取」的页面上，用户的第一反应是自己
 * 点错了。这三处对不上是上一轮导航改名时漏的：改了标签，没改标题。
 *
 * 顺带管住租户：三个页面的标题里带着「（租户：demo）」，而租户已经在
 * 侧边栏顶部常驻；「实体列表」又没带。四个页面三种写法。
 */

beforeEach(() => {
  sessionStorage.setItem('admin_session_token', 'test-token')
  localStorage.clear()
  vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
})

function renderAt(path: string) {
  return render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[path]}>
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
}

describe('每个页面的标题', () => {
  for (const item of NAV_GROUPS.flatMap((g) => g.items)) {
    it(`${item.path} 的标题是「${item.label}」`, () => {
      renderAt(item.path)
      const heading = screen.getByRole('heading', { level: 1 })
      expect(heading.textContent?.trim()).toBe(item.label)
    })
  }
})
