import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { ADMIN_ROUTES } from '../adminRoutes'

/**
 * 账号页与设置页的改密码。
 *
 * 账号页只有 admin 能用；member 直达这个 URL 看到的是无权限提示，不是
 * 404——404 会让人以为链接坏了而反复重试。
 */

let passwordChangeCalls = 0
let resetPasswordCalls: { username: string; new_password: string }[] = []

function stubApi() {
  passwordChangeCalls = 0
  resetPasswordCalls = []
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const json = (body: unknown, status = 200) =>
        Promise.resolve(new Response(JSON.stringify(body), { status }))
      if (url.includes('/auth/password')) {
        passwordChangeCalls += 1
        return json({ changed: true })
      }
      if (url.includes('/api/admin/accounts')) {
        const reset = url.match(/\/accounts\/([^/]+)\/password$/)
        if (reset && init?.method === 'PUT') {
          resetPasswordCalls.push({
            username: decodeURIComponent(reset[1]),
            ...JSON.parse(String(init.body)),
          })
          return json({ changed: true })
        }
        if (init?.method === 'POST') return json({ username: 'bob' }, 201)
        return json({
          accounts: [
            {
              username: 'admin',
              role: 'admin',
              tenant_id: null,
              status: 'active',
              created_at: '2026-09-01',
              last_login_at: '2026-09-02',
            },
            {
              username: 'alice',
              role: 'member',
              tenant_id: 'demo',
              status: 'active',
              created_at: '2026-09-01',
              last_login_at: null,
            },
          ],
        })
      }
      if (url.includes('/api/admin/tenants')) {
        return json({ tenants: [{ tenant_id: 'demo', name: '演示租户', status: 'active' }] })
      }
      if (url.includes('/nav-badges')) {
        return json({ pending_relations: 0, pending_duplicates: 0, total_terms: 0 })
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

describe('账号页', () => {
  it('admin 能看到账号列表', async () => {
    signIn('admin')
    renderAt(ADMIN_ROUTES.accounts)
    expect(await screen.findByText('alice')).toBeTruthy()
  })

  it('从未登录过和"很久没登录"是两回事，分开说', async () => {
    signIn('admin')
    renderAt(ADMIN_ROUTES.accounts)
    expect(await screen.findByText('从未登录')).toBeTruthy()
  })

  it('member 看到的是无权限提示，不是 404', async () => {
    // 404 会让人以为是链接坏了而反复重试；说清楚是权限问题，人才知道
    // 该去找谁。
    signIn('member')
    renderAt(ADMIN_ROUTES.accounts)
    expect(await screen.findByTestId('no-permission')).toBeTruthy()
    expect(screen.queryByTestId('not-found')).toBeNull()
  })

  it('新建账号要选租户', async () => {
    signIn('admin')
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.accounts)
    await user.click(await screen.findByRole('button', { name: '新建账号' }))
    expect(screen.getByLabelText('所属租户')).toBeTruthy()
    expect(screen.getByLabelText('用户名')).toBeTruthy()
    expect(screen.getByLabelText('初始密码')).toBeTruthy()
  })

  it('停用要二次确认——它会立刻把人挡在门外', async () => {
    signIn('admin')
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.accounts)
    await user.click(await screen.findByRole('button', { name: '停用 alice' }))
    // 项目的确认弹窗用 alertdialog：它要求用户回应，不是可以忽略的信息。
    expect(await screen.findByRole('alertdialog')).toBeTruthy()
  })

  it('自己那一行的停用按钮点不动', async () => {
    // 后端也会拒（400），前端禁用只是不让人白点一次。
    signIn('admin')
    renderAt(ADMIN_ROUTES.accounts)
    const own = await screen.findByRole('button', { name: '停用 admin' })
    expect(own.hasAttribute('disabled')).toBe(true)
  })
})

describe('设置页的改密码', () => {
  it('两种角色都有', async () => {
    for (const role of ['admin', 'member'] as const) {
      sessionStorage.clear()
      signIn(role)
      const { unmount } = renderAt(ADMIN_ROUTES.settings)
      expect(await screen.findByLabelText('原密码')).toBeTruthy()
      expect(screen.getByLabelText('新密码')).toBeTruthy()
      expect(screen.getByLabelText('确认新密码')).toBeTruthy()
      unmount()
    }
  })

  it('两次新密码不一致时不发请求', async () => {
    // 这个错误后端无从判断（它只收到一个新密码），发过去只会成功改成
    // 打错的那个，然后你就登不进来了。
    signIn('member')
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.settings)
    await user.type(await screen.findByLabelText('原密码'), 'password1')
    await user.type(screen.getByLabelText('新密码'), 'password2')
    await user.type(screen.getByLabelText('确认新密码'), 'password3')
    await user.click(screen.getByRole('button', { name: '修改密码' }))
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(passwordChangeCalls).toBe(0)
  })

  it('一致时才发请求', async () => {
    signIn('member')
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.settings)
    await user.type(await screen.findByLabelText('原密码'), 'password1')
    await user.type(screen.getByLabelText('新密码'), 'password2')
    await user.type(screen.getByLabelText('确认新密码'), 'password2')
    await user.click(screen.getByRole('button', { name: '修改密码' }))
    await waitFor(() => expect(passwordChangeCalls).toBe(1))
  })
})

describe('重置密码', () => {
  it('admin 能给别人重置密码，不需要旧密码', async () => {
    // 这个接口就是给"忘了密码"用的——要旧密码就等于不能重置。没有它，
    // admin 在界面上帮不了忘记密码的人，只能手改数据库。
    signIn('admin')
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.accounts)
    await user.click(await screen.findByRole('button', { name: '重置 alice 的密码' }))
    await user.type(screen.getByLabelText('新密码'), 'brandnewpass')
    await user.click(screen.getByRole('button', { name: '确认重置' }))
    await waitFor(() => expect(resetPasswordCalls).toEqual([{ username: 'alice', new_password: 'brandnewpass' }]))
  })

  it('取消就不发请求', async () => {
    signIn('admin')
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.accounts)
    await user.click(await screen.findByRole('button', { name: '重置 alice 的密码' }))
    await user.click(screen.getByRole('button', { name: '取消重置' }))
    expect(resetPasswordCalls).toEqual([])
  })
})
