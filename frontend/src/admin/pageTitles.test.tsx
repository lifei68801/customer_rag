import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { NAV_GROUPS } from '../adminRoutes'
import { resetAdminSession } from './useAdminAuth'

/**
 * 身份不再存 sessionStorage（token 在 HttpOnly Cookie 里，JS 读不到，也
 * 塞不进去）：界面从 whoami 拿身份，所以这里要打桩的是 whoami。
 */
function whoamiResponse() {
  return Promise.resolve(
    new Response(
      JSON.stringify({
        username: 'alice',
        role: 'member',
        tenant_id: 'demo',
        current_tenant_id: 'demo',
      }),
      { status: 200 },
    ),
  )
}

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
  resetAdminSession()
  localStorage.clear()
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) =>
      String(input).includes('/auth/whoami') ? whoamiResponse() : new Promise(() => {}),
    ),
  )
})

// 会话状态是异步的（身份从 whoami 读，token 在 HttpOnly Cookie 里 JS 读不
// 到），后台外壳要等 whoami 回来才画得出来。不等的话断言会对着一棵空树跑。
async function renderAt(path: string) {
  const result = render(
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
  await screen.findByTestId('admin-topbar')
  return result
}

describe('每个页面的标题', () => {
  for (const item of NAV_GROUPS.flatMap((g) => g.items)) {
    it(`${item.path} 的标题是「${item.label}」`, async () => {
      await renderAt(item.path)
      const heading = screen.getByRole('heading', { level: 1 })
      expect(heading.textContent?.trim()).toBe(item.label)
    })
  }
})
