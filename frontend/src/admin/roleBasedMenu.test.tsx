import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, renderHook, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { TenantProvider, useAdminTenant } from './TenantContext'
import { ADMIN_ROUTES } from '../adminRoutes'

/**
 * 账号菜单按角色渲染。
 *
 * member 看不到租户切换——不是因为按钮被藏起来了，而是因为这个能力对它
 * 不存在（后端 403）。前端隐藏只是不去误导人。
 */

function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      const json = (body: unknown) =>
        Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))
      if (url.includes('/api/admin/tenants')) {
        return json({
          tenants: [
            { tenant_id: 'demo', name: '演示租户', status: 'active' },
            { tenant_id: 'acme', name: 'ACME', status: 'active' },
          ],
        })
      }
      if (url.includes('/nav-badges')) {
        return json({ pending_relations: 0, pending_duplicates: 0, total_terms: 0 })
      }
      if (url.includes('/documents')) {
        return json({ documents: [], total: 0, pending_jobs: [], dead_jobs: [] })
      }
      return new Promise(() => {})
    }),
  )
}

function signIn(role: 'admin' | 'member') {
  sessionStorage.setItem('admin_session_token', 'tok')
  sessionStorage.setItem('admin_username', role === 'admin' ? 'admin' : 'alice')
  sessionStorage.setItem('admin_role', role)
  sessionStorage.setItem('admin_current_tenant', 'demo')
}

beforeEach(() => {
  sessionStorage.clear()
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

const trigger = () => screen.getByRole('button', { name: /账号与租户/ })
const menu = () => within(screen.getByRole('menu', { name: '账号与租户' }))

describe('member 的菜单', () => {
  beforeEach(() => signIn('member'))

  it('没有租户切换、账号管理、租户管理', async () => {
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.documents)
    await user.click(trigger())
    expect(menu().queryByRole('menuitemradio')).toBeNull()
    expect(menu().queryByRole('menuitem', { name: '账号管理' })).toBeNull()
    expect(menu().queryByRole('menuitem', { name: '租户管理' })).toBeNull()
  })

  it('设置和登出还在——菜单不是整个消失', async () => {
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.documents)
    await user.click(trigger())
    expect(menu().getByRole('menuitem', { name: '设置' })).toBeTruthy()
    expect(menu().getByRole('menuitem', { name: '登出' })).toBeTruthy()
  })

  it('触发按钮同时显示租户名和用户名', async () => {
    renderAt(ADMIN_ROUTES.documents)
    await waitFor(() => expect(trigger().textContent).toMatch(/演示租户/))
    expect(trigger().textContent).toMatch(/alice/)
  })

  it('setTenantId 不生效——它不只是被藏起来了', () => {
    const { result } = renderHook(() => useAdminTenant(), {
      wrapper: ({ children }) => <TenantProvider>{children}</TenantProvider>,
    })

    act(() => result.current.setTenantId('acme'))

    // 藏起来的按钮还能被别的代码路径调用到；这个能力必须真的不存在。
    expect(result.current.tenantId).toBe('demo')
    expect(sessionStorage.getItem('admin_current_tenant')).toBe('demo')
  })
})

describe('admin 的菜单', () => {
  beforeEach(() => signIn('admin'))

  it('该有的都在', async () => {
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.documents)
    await waitFor(() => expect(trigger().textContent).toMatch(/演示租户/))
    await user.click(trigger())
    expect(menu().getAllByRole('menuitemradio').length).toBe(2)
    for (const name of ['账号管理', '租户管理', '设置', '登出']) {
      expect(menu().getByRole('menuitem', { name })).toBeTruthy()
    }
  })

  it('菜单里不再有「新建租户」——它归租户管理页', async () => {
    // 这个菜单管的是"我是谁、我在哪个租户"，新建是一次性的管理动作，
    // 属于租户管理页。同一个动作留两个入口，改起来就得记得改两处。
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.documents)
    await waitFor(() => expect(trigger().textContent).toMatch(/演示租户/))
    await user.click(trigger())
    expect(menu().queryByRole('menuitem', { name: '新建租户' })).toBeNull()
    expect(menu().queryByRole('button', { name: '新建租户' })).toBeNull()
  })

  it('可以切换到另一个租户', async () => {
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.documents)
    await waitFor(() => expect(trigger().textContent).toMatch(/演示租户/))
    await user.click(trigger())
    await user.click(menu().getByRole('menuitemradio', { name: /ACME/ }))
    await waitFor(() => expect(sessionStorage.getItem('admin_current_tenant')).toBe('acme'))
  })

  it('触发按钮显示当前租户和 admin', async () => {
    renderAt(ADMIN_ROUTES.documents)
    await waitFor(() => expect(trigger().textContent).toMatch(/演示租户/))
    expect(trigger().textContent).toMatch(/admin/)
  })
})
