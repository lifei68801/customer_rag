import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { resetAdminSession } from './useAdminAuth'

/**
 * 登录从"一个共享 token"换成用户名 + 密码。
 *
 * 登录响应里的 role/tenant_id 决定前端渲染什么——但渲染不承担安全责任，
 * 真正的门在后端的 require_tenant_access 上。
 */

let lastBody: unknown = null
let loggedIn = false

let loginDetail = '用户名或密码不正确'

function stubLogin(status: number | 'network-error' = 200, detail = '用户名或密码不正确') {
  lastBody = null
  loggedIn = false
  loginDetail = detail
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      // 登录成功之后身份从 whoami 读——响应体里那份 session_token 前端不再
      // 存任何地方，Cookie 由服务端下发。
      if (url.includes('/auth/whoami')) {
        return Promise.resolve(
          loggedIn
            ? new Response(
                JSON.stringify({
                  username: 'alice',
                  role: 'member',
                  tenant_id: 'demo',
                  current_tenant_id: 'demo',
                }),
                { status: 200 },
              )
            : new Response(JSON.stringify({ detail: '未登录' }), { status: 401 }),
        )
      }
      if (url.includes('/auth/login')) {
        lastBody = JSON.parse(String(init?.body))
        // 网络层直接失败（后端没起来、代理连不上）——fetch 会 reject，
        // 不是返回一个 !ok 的 Response。这条路径以前一条用例都没有。
        if (status === 'network-error') return Promise.reject(new TypeError('Failed to fetch'))
        if (status !== 200) {
          return Promise.resolve(new Response(JSON.stringify({ detail: loginDetail }), { status }))
        }
        loggedIn = true
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
  resetAdminSession()
  sessionStorage.clear()
  localStorage.clear()
  stubLogin()
})

async function renderLogin() {
  const result = render(
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
  // 会话状态未知时登录页先不画（把表单闪给一个其实还登录着的人，他会以为
  // 自己被登出了），等 whoami 回来。
  await screen.findByLabelText('用户名')
  return result
}

async function submit(user: ReturnType<typeof userEvent.setup>, name: string, password: string) {
  await user.type(screen.getByLabelText('用户名'), name)
  await user.type(screen.getByLabelText('密码'), password)
  await user.click(screen.getByRole('button', { name: '登录' }))
}

describe('登录页', () => {
  it('有用户名和密码两个输入框', async () => {
    await renderLogin()
    expect(screen.getByLabelText('用户名')).toBeTruthy()
    expect(screen.getByLabelText('密码')).toBeTruthy()
    // 旧的单字段登录必须绝迹——留着它会让人以为还能用 token 登录。
    expect(screen.queryByLabelText('管理员 token')).toBeNull()
  })

  it('提交用户名和密码，不是 admin_token', async () => {
    const user = userEvent.setup()
    await renderLogin()
    await submit(user, 'alice', 'password1')
    await waitFor(() => expect(lastBody).not.toBeNull())
    expect(lastBody).toEqual({ username: 'alice', password: 'password1' })
  })

  it('登录成功后身份从 whoami 取', async () => {
    // 登录响应里那份 session_token 前端不再存任何地方——sessionStorage 按
    // 标签页隔离，存在那里的话同一个人开两个标签页会看到两份不一样的身份。
    // 这里不去断言那几个键是空的：已经没有任何代码写它们了，正确实现和错误
    // 实现都会通过。能钉住的是「身份确实从 whoami 读到了」。
    const user = userEvent.setup()
    await renderLogin()
    await submit(user, 'alice', 'password1')
    // 登录页在已登录时会跳走，用它确认身份真的读到了。
    expect(await screen.findByTestId('admin-topbar')).toBeTruthy()
  })

  it('失败时显示错误，且不写入任何身份', async () => {
    stubLogin(401)
    const user = userEvent.setup()
    await renderLogin()
    await submit(user, 'alice', 'wrongpassword')
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    // 半写入的身份比完全没有更糟：页面会以为已登录，然后每个请求都 401。
    // 登录失败后还留在登录页，说明没有任何一半的身份被认下来。
    expect(screen.getByLabelText('用户名')).toBeTruthy()
  })

  it('后端连不上时说的是连不上，不是密码不对', async () => {
    // 这是 2026-09-11 真实发生过的那次：后端进程没起来，用户在登录页看到
    // 「用户名或密码不正确」，于是反复试密码、怀疑自己记错了——而真正的
    // 问题是服务没跑。把网络故障说成凭据错误，是把人往错的方向推。
    stubLogin('network-error')
    const user = userEvent.setup()
    await renderLogin()
    await submit(user, 'alice', 'whatever')
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/连不上|无法连接|服务/)
    expect(alert.textContent).not.toMatch(/密码/)
  })

  it('服务端 500 时说的是服务出错，不是密码不对', async () => {
    stubLogin(500, '内部错误')
    const user = userEvent.setup()
    await renderLogin()
    await submit(user, 'alice', 'whatever')
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).not.toMatch(/密码/)
  })

  it('被限流锁定时把后端那句话原样转达', async () => {
    // 后端连错 5 次会锁 15 分钟并回 429。前端说成「密码不正确」的话，
    // 用户会继续试——而每次尝试都在把锁定时间续上，他永远出不来。
    //
    // 这里不怕"泄露用户名存在"：后端本来就只对存在的用户计数、429 状态码
    // 本身就带着这个信息，攻击者直接读状态码，不看界面。前端瞒着它保护不了
    // 任何人，只会坑住真正的用户。
    stubLogin(429, '尝试太频繁，账号已锁定 15 分钟')
    const user = userEvent.setup()
    await renderLogin()
    await submit(user, 'alice', 'whatever')
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/15 分钟|锁定/)
  })

  it('401 仍然只说「用户名或密码不正确」，不区分是哪一种', async () => {
    // 这条是**反面**：后端刻意不区分"用户不存在/密码错/账号禁用"，否则登录
    // 接口就成了用户名枚举器。上面三条放开的是**非凭据**失败，不能顺手把
    // 这条一起放开。
    stubLogin(401, '用户不存在')
    const user = userEvent.setup()
    await renderLogin()
    await submit(user, 'alice', 'whatever')
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/用户名或密码不正确/)
    // 后端那句更具体的原因绝不能透出去。
    expect(alert.textContent).not.toMatch(/用户不存在/)
  })

  it('密码框是 password 类型', async () => {
    await renderLogin()
    expect(screen.getByLabelText('密码').getAttribute('type')).toBe('password')
  })
})
