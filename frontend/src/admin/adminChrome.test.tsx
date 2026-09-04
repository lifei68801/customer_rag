import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { ADMIN_ROUTES } from '../adminRoutes'
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
 * 前台 ⇄ 后台的往返入口。
 *
 * 前台右上角是「管理后台」，后台右上角就该是「返回前台」——两个方向的
 * 入口落在屏幕上的同一个点，用户不用为回去这件事重新找一遍。此前「返回
 * 前台」藏在左下角的账号菜单里：去处对了，位置不对，而且要点两下。
 */

function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/auth/whoami')) return whoamiResponse()
      if (url.includes('/api/admin/tenants')) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ tenants: [{ tenant_id: 'demo', name: '演示租户', status: 'active' }] }),
            { status: 200 },
          ),
        )
      }
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  resetAdminSession()
  localStorage.clear()
  stubApi()
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

/** 顶栏里最靠右的那个元素。flex + justify-between 下，最后一个子节点就是右端。 */
function rightmostOf(testId: string): Element {
  const bar = screen.getByTestId(testId)
  const last = bar.lastElementChild
  if (!last) throw new Error(`${testId} 是空的`)
  return last
}

describe('返回前台常驻在后台右上角', () => {
  // 每个页面都要在。只在某几个页面出现的话，用户会在缺它的那页以为自己
  // 走进了没有出口的地方。
  for (const [name, path] of [
    ['本体结构', ADMIN_ROUTES.ontology],
    ['文档', ADMIN_ROUTES.documents],
    ['实体列表', ADMIN_ROUTES.terms],
    ['设置', ADMIN_ROUTES.settings],
  ] as const) {
    it(`${name}页在顶栏右端有「返回前台」`, async () => {
      renderAt(path)
      const link = await waitFor(() => within(screen.getByTestId('admin-topbar')).getByRole('link', { name: '返回前台' }))
      expect(link.getAttribute('href')).toBe('/')
      // 位置断言：它必须是顶栏最靠右的元素，不能只是"在顶栏里的某处"。
      expect(rightmostOf('admin-topbar').contains(link)).toBe(true)
    })
  }

  it('和前台的「管理后台」落在同一个位置——都是各自顶栏的右端', () => {
    renderAt('/')
    const entry = within(screen.getByTestId('site-topbar')).getByRole('link', {
      name: '管理后台',
    })
    expect(entry.getAttribute('href')).toBe('/admin')
    expect(rightmostOf('site-topbar').contains(entry)).toBe(true)
  })

  it('账号菜单里不再重复一份', async () => {
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.documents)
    await waitFor(() => expect(screen.getByRole('button', { name: /账号与租户/ }).textContent).toMatch(/演示租户/))
    await user.click(screen.getByRole('button', { name: /账号与租户/ }))
    const menu = within(screen.getByRole('menu', { name: '账号与租户' }))
    // 同一个动作两个入口，用户会以为它们不是一回事。
    expect(menu.queryByRole('menuitem', { name: '返回前台' })).toBeNull()
    // 别的项还在——这条是防止我"清理"过头把整段菜单删了。
    expect(menu.getByRole('menuitem', { name: '设置' })).toBeTruthy()
    expect(menu.getByRole('menuitem', { name: '登出' })).toBeTruthy()
  })
})
