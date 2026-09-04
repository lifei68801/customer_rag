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
import { resetAdminSession } from './useAdminAuth'

/**
 * 身份不再存 sessionStorage（token 在 HttpOnly Cookie 里，JS 读不到，也
 * 塞不进去）：界面从 whoami 拿身份，所以这里要打桩的是 whoami。
 */
let signedInRole: 'admin' | 'member' | null = null

function whoamiResponse() {
  if (signedInRole === null) {
    return Promise.resolve(new Response(JSON.stringify({ detail: '未登录' }), { status: 401 }))
  }
  return Promise.resolve(
    new Response(
      JSON.stringify({
        username: signedInRole === 'admin' ? 'admin' : 'alice',
        role: signedInRole,
        tenant_id: signedInRole === 'admin' ? null : 'demo',
        current_tenant_id: 'demo',
      }),
      { status: 200 },
    ),
  )
}

function signIn(role: 'admin' | 'member') {
  signedInRole = role
}

/**
 * 账号菜单按角色渲染。
 *
 * member 看不到租户切换——不是因为按钮被藏起来了，而是因为这个能力对它
 * 不存在（后端 403）。前端隐藏只是不去误导人。
 */

let requests: { url: string; method: string }[] = []

function stubApi() {
  requests = []
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/auth/whoami')) return whoamiResponse()
      requests.push({ url, method: (init?.method ?? 'GET').toUpperCase() })
      if (url.includes('/auth/session/tenant')) {
        return Promise.resolve(new Response(JSON.stringify({}), { status: 200 }))
      }
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

beforeEach(() => {
  signedInRole = null
  resetAdminSession()
  sessionStorage.clear()
  localStorage.clear()
  stubApi()
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

const trigger = () => screen.getByRole('button', { name: /账号与租户/ })
const menu = () => within(screen.getByRole('menu', { name: '账号与租户' }))

describe('member 的菜单', () => {
  beforeEach(() => signIn('member'))

  it('没有租户切换、账号管理、租户管理', async () => {
    const user = userEvent.setup()
    await renderAt(ADMIN_ROUTES.documents)
    await user.click(trigger())
    expect(menu().queryByRole('menuitemradio')).toBeNull()
    expect(menu().queryByRole('menuitem', { name: '账号管理' })).toBeNull()
    expect(menu().queryByRole('menuitem', { name: '租户管理' })).toBeNull()
  })

  it('设置和登出还在——菜单不是整个消失', async () => {
    const user = userEvent.setup()
    await renderAt(ADMIN_ROUTES.documents)
    await user.click(trigger())
    expect(menu().getByRole('menuitem', { name: '设置' })).toBeTruthy()
    expect(menu().getByRole('menuitem', { name: '登出' })).toBeTruthy()
  })

  it('触发按钮同时显示租户名和用户名', async () => {
    await renderAt(ADMIN_ROUTES.documents)
    await waitFor(() => expect(trigger().textContent).toMatch(/演示租户/))
    expect(trigger().textContent).toMatch(/alice/)
  })

  it('setTenantId 不生效——它不只是被藏起来了', async () => {
    const { result } = renderHook(() => useAdminTenant(), {
      // ToastProvider 是 TenantProvider 的依赖（切租户失败要说出来），
      // 站点里它挂在 main.tsx 的根节点上。
      wrapper: ({ children }) => (
        <ToastProvider>
          <TenantProvider>{children}</TenantProvider>
        </ToastProvider>
      ),
    })
    // 租户来自 whoami，等它回来再动手。
    await waitFor(() => expect(result.current.tenantId).toBe('demo'))

    act(() => result.current.setTenantId('acme'))

    // 藏起来的按钮还能被别的代码路径调用到；这个能力必须真的不存在：
    // 连那个 PUT 都不该发出去。
    expect(result.current.tenantId).toBe('demo')
    expect(requests.some((r) => r.url.includes('/session/tenant'))).toBe(false)
  })
})

describe('admin 的菜单', () => {
  beforeEach(() => signIn('admin'))

  it('该有的都在', async () => {
    const user = userEvent.setup()
    await renderAt(ADMIN_ROUTES.documents)
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
    await renderAt(ADMIN_ROUTES.documents)
    await waitFor(() => expect(trigger().textContent).toMatch(/演示租户/))
    await user.click(trigger())
    expect(menu().queryByRole('menuitem', { name: '新建租户' })).toBeNull()
    expect(menu().queryByRole('button', { name: '新建租户' })).toBeNull()
  })

  it('可以切换到另一个租户', async () => {
    const user = userEvent.setup()
    await renderAt(ADMIN_ROUTES.documents)
    await waitFor(() => expect(trigger().textContent).toMatch(/演示租户/))
    await user.click(trigger())
    await user.click(menu().getByRole('menuitemradio', { name: /ACME/ }))
    // 切换走服务端：本地状态是这个 PUT 成功之后才跟上的。
    await waitFor(() =>
      expect(requests.some((r) => r.url.includes('/session/tenant') && r.method === 'PUT')).toBe(true),
    )
    await waitFor(() => expect(trigger().textContent).toMatch(/ACME/))
  })

  it('触发按钮显示当前租户和 admin', async () => {
    await renderAt(ADMIN_ROUTES.documents)
    await waitFor(() => expect(trigger().textContent).toMatch(/演示租户/))
    expect(trigger().textContent).toMatch(/admin/)
  })
})
