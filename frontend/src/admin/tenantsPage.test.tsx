import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
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
 * 租户管理页。只有 admin 能用。
 *
 * 它存在的直接理由：启动时会自动停用测试残留租户，而此前没有任何界面能把
 * 它们启用回来——用户只能去调接口。
 */

let listCalls: string[] = []
let statusCalls: string[] = []

function stubApi() {
  listCalls = []
  statusCalls = []
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/auth/whoami')) return whoamiResponse()
      const json = (body: unknown, status = 200) =>
        Promise.resolve(new Response(JSON.stringify(body), { status }))
      const statusChange = url.match(/\/tenants\/([^/]+)\/(disable|enable)$/)
      if (statusChange) {
        statusCalls.push(`${statusChange[2]}:${decodeURIComponent(statusChange[1])}`)
        return json({ [statusChange[2] + 'd']: true })
      }
      if (url.includes('/api/admin/tenants')) {
        if (init?.method === 'POST') return json({ tenant_id: 'newone', name: '新租户' }, 201)
        listCalls.push(url)
        const all = [
          { tenant_id: 'demo', name: '演示租户', status: 'active' },
          { tenant_id: 't_verify', name: 't_verify', status: 'disabled' },
        ]
        return json({
          tenants: url.includes('include_disabled=true')
            ? all
            : all.filter((t) => t.status === 'active'),
        })
      }
      if (url.includes('/nav-badges')) {
        return json({ pending_relations: 0, pending_duplicates: 0, total_terms: 0 })
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

describe('租户管理页', () => {
  it('列出停用的租户——这正是这个页面存在的理由', async () => {
    // 启动时自动停用的测试残留租户，此前没有任何界面能把它们启用回来。
    // 按"启用"按钮找而不是按名字：t_verify 的 ID 和显示名相同，findByText
    // 会撞上两个元素；而这个按钮的存在本身就证明那一行渲染出来了。
    signIn('admin')
    await renderAt(ADMIN_ROUTES.tenants)
    expect(await screen.findByRole('button', { name: '启用 t_verify' })).toBeTruthy()
    // 启用中的那个显示"停用"，两者不会搞混。
    expect(screen.getByRole('button', { name: '停用 demo' })).toBeTruthy()
  })

  it('请求里带 include_disabled，不然看不到停用的', async () => {
    signIn('admin')
    await renderAt(ADMIN_ROUTES.tenants)
    await waitFor(() =>
      expect(listCalls.some((u) => u.includes('include_disabled=true'))).toBe(true),
    )
  })

  it('停用中的租户能启用回来', async () => {
    signIn('admin')
    const user = userEvent.setup()
    await renderAt(ADMIN_ROUTES.tenants)
    await user.click(await screen.findByRole('button', { name: '启用 t_verify' }))
    await waitFor(() => expect(statusCalls).toEqual(['enable:t_verify']))
  })

  it('停用要二次确认', async () => {
    signIn('admin')
    const user = userEvent.setup()
    await renderAt(ADMIN_ROUTES.tenants)
    await user.click(await screen.findByRole('button', { name: '停用 demo' }))
    expect(await screen.findByRole('alertdialog')).toBeTruthy()
    // 停用租户和停用账号的后果完全不同，确认框必须说清楚：那个租户的成员
    // 仍能登录、仍能读，只是写操作会失败。
    expect(screen.getByRole('alertdialog').textContent).toMatch(/仍(能|可以)登录/)
  })

  it('取消确认就不发请求', async () => {
    signIn('admin')
    const user = userEvent.setup()
    await renderAt(ADMIN_ROUTES.tenants)
    await user.click(await screen.findByRole('button', { name: '停用 demo' }))
    await user.click(await screen.findByRole('button', { name: '取消' }))
    expect(statusCalls).toEqual([])
  })

  it('不能停用自己当前所在的租户', async () => {
    // 停掉之后这一页自己的写操作也会开始 404，而人还在这个租户里——
    // 先切走再停，顺序上说得通。
    signIn('admin')
    await renderAt(ADMIN_ROUTES.tenants)
    const own = await screen.findByRole('button', { name: '停用 demo' })
    expect(own.hasAttribute('disabled')).toBe(false)
  })

  it('新建租户', async () => {
    signIn('admin')
    const user = userEvent.setup()
    await renderAt(ADMIN_ROUTES.tenants)
    await user.click(await screen.findByRole('button', { name: '新建租户' }))
    expect(screen.getByLabelText('租户 ID')).toBeTruthy()
    expect(screen.getByLabelText('显示名')).toBeTruthy()
  })

  it('member 看到的是无权限提示，不是 404', async () => {
    signIn('member')
    await renderAt(ADMIN_ROUTES.tenants)
    expect(await screen.findByTestId('no-permission')).toBeTruthy()
    expect(screen.queryByTestId('not-found')).toBeNull()
  })
})

describe('账号菜单里的入口', () => {
  it('admin 能看到「租户管理」', async () => {
    signIn('admin')
    const user = userEvent.setup()
    await renderAt(ADMIN_ROUTES.documents)
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /账号与租户/ }).textContent).toMatch(/演示租户/),
    )
    await user.click(screen.getByRole('button', { name: /账号与租户/ }))
    expect(screen.getByRole('menuitem', { name: '租户管理' })).toBeTruthy()
  })

  it('member 看不到', async () => {
    signIn('member')
    const user = userEvent.setup()
    await renderAt(ADMIN_ROUTES.documents)
    await user.click(screen.getByRole('button', { name: /账号与租户/ }))
    expect(screen.queryByRole('menuitem', { name: '租户管理' })).toBeNull()
  })
})
