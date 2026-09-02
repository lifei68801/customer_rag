import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'

/**
 * 登录从"一个共享 token"换成用户名 + 密码。
 *
 * 登录响应里的 role/tenant_id 决定前端渲染什么——但渲染不承担安全责任，
 * 真正的门在后端的 require_tenant_access 上。
 */

let lastBody: unknown = null

function stubLogin(status = 200) {
  lastBody = null
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/auth/login')) {
        lastBody = JSON.parse(String(init?.body))
        if (status !== 200) {
          return Promise.resolve(
            new Response(JSON.stringify({ detail: '用户名或密码不正确' }), { status }),
          )
        }
        return Promise.resolve(
          new Response(
            JSON.stringify({
              session_token: 'tok',
              username: 'alice',
              role: 'member',
              tenant_id: 'demo',
            }),
            { status: 200 },
          ),
        )
      }
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  stubLogin()
})

function renderLogin() {
  return render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={['/admin/login']}>
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
}

async function submit(user: ReturnType<typeof userEvent.setup>, name: string, password: string) {
  await user.type(screen.getByLabelText('用户名'), name)
  await user.type(screen.getByLabelText('密码'), password)
  await user.click(screen.getByRole('button', { name: '登录' }))
}

describe('登录页', () => {
  it('有用户名和密码两个输入框', () => {
    renderLogin()
    expect(screen.getByLabelText('用户名')).toBeTruthy()
    expect(screen.getByLabelText('密码')).toBeTruthy()
    // 旧的单字段登录必须绝迹——留着它会让人以为还能用 token 登录。
    expect(screen.queryByLabelText('管理员 token')).toBeNull()
  })

  it('提交用户名和密码，不是 admin_token', async () => {
    const user = userEvent.setup()
    renderLogin()
    await submit(user, 'alice', 'password1')
    await waitFor(() => expect(lastBody).not.toBeNull())
    expect(lastBody).toEqual({ username: 'alice', password: 'password1' })
  })

  it('登录成功后身份存进 sessionStorage', async () => {
    const user = userEvent.setup()
    renderLogin()
    await submit(user, 'alice', 'password1')
    await waitFor(() => expect(sessionStorage.getItem('admin_session_token')).toBe('tok'))
    expect(sessionStorage.getItem('admin_username')).toBe('alice')
    expect(sessionStorage.getItem('admin_role')).toBe('member')
    // member 的租户由登录响应决定，不是上次留下的那个。
    expect(sessionStorage.getItem('admin_current_tenant')).toBe('demo')
  })

  it('失败时显示错误，且不写入任何身份', async () => {
    stubLogin(401)
    const user = userEvent.setup()
    renderLogin()
    await submit(user, 'alice', 'wrongpassword')
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    // 半写入的身份比完全没有更糟：页面会以为已登录，然后每个请求都 401。
    expect(sessionStorage.getItem('admin_session_token')).toBeNull()
    expect(sessionStorage.getItem('admin_role')).toBeNull()
    expect(sessionStorage.getItem('admin_username')).toBeNull()
  })

  it('密码框是 password 类型', async () => {
    renderLogin()
    expect(screen.getByLabelText('密码').getAttribute('type')).toBe('password')
  })
})
