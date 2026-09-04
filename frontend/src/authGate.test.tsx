import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import App from './App'
import { SkinProvider } from './admin/SkinContext'
import { ConfirmProvider } from './admin/ConfirmContext'
import { ToastProvider } from './admin/ToastContext'
import { resetAdminSession } from './admin/useAdminAuth'

/**
 * 前台（`/`）的登录门与账号块。
 *
 * 前台此前完全没有身份：租户是硬编码的 'demo'，用户是 localStorage 里的
 * 随机 UUID。服务端五个前台接口现在都要会话，前台不登录就是全线 401。
 *
 * 身份从 whoami 拿（token 在 HttpOnly Cookie 里，JS 读不到也塞不进去），
 * 所以这里要打桩的是 whoami。
 */

/** SSE 的事件分隔符是两个换行。用 fromCharCode 拼，免得转义写错还看不出来。 */
const NEWLINE = String.fromCharCode(10)
const SSE_FINAL =
  'data: ' +
  JSON.stringify({ type: 'final', text: '好的', used_sources: [] }) +
  NEWLINE +
  NEWLINE

interface Whoami {
  username: string
  role: 'admin' | 'member'
  tenant_id: string | null
  current_tenant_id: string | null
}

function stubApi({
  whoami,
  tenants = [{ tenant_id: 'demo', name: 'demo', status: 'active' }],
  sessionsStatus = 200,
}: {
  whoami: Whoami | 401
  tenants?: { tenant_id: string; name: string; status: string }[]
  /** 会话列表接口的状态码。401 用来演「服务端已经不认这个会话了」。 */
  sessionsStatus?: number
}) {
  const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/auth/whoami')) {
        return Promise.resolve(
          whoami === 401
            ? new Response('{}', { status: 401 })
            : new Response(JSON.stringify(whoami), { status: 200 }),
        )
      }
      if (url.includes('/auth/session/tenant')) {
        return Promise.resolve(new Response('{}', { status: 200 }))
      }
      if (url.includes('/api/admin/tenants')) {
        return Promise.resolve(new Response(JSON.stringify({ tenants }), { status: 200 }))
      }
      if (url.includes('/agent/sessions')) {
        return Promise.resolve(
          new Response(JSON.stringify({ sessions: [] }), { status: sessionsStatus }),
        )
      }
      if (url.includes('/agent/chat')) {
        return Promise.resolve(
          new Response(SSE_FINAL, {
            status: 200,
          }),
        )
      }
      return new Promise<Response>(() => {})
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

beforeEach(() => {
  // 会话状态在模块级，同一个文件里跨用例存活——不重置的话上一条用例
  // 登录出来的身份会漏进下一条。
  resetAdminSession()
  localStorage.clear()
  // CSRF 令牌那个 Cookie 刻意不是 HttpOnly，前端读得到、要塞进请求头。
  document.cookie = 'customer_rag_csrf=token-abc'
})

/** 落点探针。断言「没被弹走」必须看地址本身，看页面内容会假绿。 */
function LocationProbe() {
  return <span data-testid="pathname">{useLocation().pathname}</span>
}

function renderAt(path: string) {
  // 这三个 Provider 挂在 main.tsx 的根节点（站点级能力，前台后台共用），
  // 不在 App 内部，所以测试要自己补上。
  return render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[path]}>
            <LocationProbe />
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
}

describe('前台登录门', () => {
  it('未登录时前台渲染登录表单，不渲染问答界面', async () => {
    stubApi({ whoami: 401 })
    renderAt('/')
    expect(await screen.findByLabelText(/用户名/)).toBeTruthy()
    expect(screen.queryByPlaceholderText(/输入你的问题/)).toBeNull()
  })
})

describe('前台账号块', () => {
  it('登录后前台显示账号块，但没有账号管理和租户管理', async () => {
    // 前台是「用知识库」的地方，后台是「管知识库」的地方。把管理入口塞进
    // 问答界面，等于把建模→接入→审核这条流程的入口散回一个不属于它的页面。
    stubApi({
      whoami: { username: 'admin', role: 'admin', tenant_id: null, current_tenant_id: 'demo' },
    })
    renderAt('/')
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: /admin/ }))
    expect(screen.getByRole('menuitem', { name: '设置' })).toBeTruthy()
    expect(screen.queryByRole('menuitem', { name: '账号管理' })).toBeNull()
    expect(screen.queryByRole('menuitem', { name: '租户管理' })).toBeNull()
  })

  it('前台给 admin 显示租户切换器', async () => {
    // 换租户即换知识库，admin 需要验证「我刚配好的本体，问答到底通不通」。
    stubApi({
      whoami: { username: 'admin', role: 'admin', tenant_id: null, current_tenant_id: 'demo' },
    })
    renderAt('/')
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: /admin/ }))
    expect(await screen.findByRole('menuitemradio', { name: /demo/ })).toBeTruthy()
  })

  it('member 看不到租户切换器', async () => {
    stubApi({
      whoami: { username: 'alice', role: 'member', tenant_id: 'demo', current_tenant_id: 'demo' },
    })
    renderAt('/')
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: /alice/ }))
    // 正向锚点：先钉住菜单确实打开了、里面确实有 member 该看到的东西。
    // 少了这一句，把触发按钮的 onClick 改成空函数（菜单永远打不开）时
    // 下面那条照样绿——评审实测过。
    expect(screen.getByRole('menuitem', { name: '登出' })).toBeTruthy()
    expect(screen.queryByRole('menuitemradio')).toBeNull()
  })
})

describe('还没选租户', () => {
  it('把「请先选择一个租户」摆出来，而不是一片什么都没有的问答界面', async () => {
    // admin 的 tenant_id 恒为 None，当前租户要显式切过一次才有值。在那
    // 之前前台每个请求都会撞上后端的 400「请先选择一个租户」。
    //
    // 租户列表给空的：useTenants 的「当前租户不在列表里就自动纠正」只在
    // 列表非空时才动得了，所以这里没有东西会替用户把它补上——正是需要
    // 用户自己看见并纠正的那个状态。
    stubApi({
      whoami: { username: 'admin', role: 'admin', tenant_id: null, current_tenant_id: null },
      tenants: [],
    })
    renderAt('/')

    expect(await screen.findByText(/请先选择一个租户/)).toBeTruthy()
    expect(screen.queryByPlaceholderText(/输入你的问题/)).toBeNull()
    // 光说「没选」不够：纠正它的地方必须同时在屏幕上。
    expect(screen.getByRole('button', { name: /admin/ })).toBeTruthy()
  })

  it('账号块不声称有一个当前租户', async () => {
    // 同一屏上正文说「请先选择一个租户」、账号块说「当前 demo」，这就是
    // 界面显示的和实际生效的脱钩：那个 demo 来自 TenantContext 的兜底值，
    // 服务端会话里什么都没有。用户会以为自己已经在 demo 里了。
    stubApi({
      whoami: { username: 'admin', role: 'admin', tenant_id: null, current_tenant_id: null },
      tenants: [],
    })
    renderAt('/')
    const user = userEvent.setup()

    const account = await screen.findByRole('button', { name: /admin/ })
    expect(account.getAttribute('aria-label')).toBe('账号与租户，未选择租户，登录为 admin')

    await user.click(account)
    // 正向锚点：菜单确实打开了、切换器确实列出了可选项——只是没有一项
    // 被标成当前。
    expect(screen.getByRole('menuitemradio', { name: /demo/ })).toBeTruthy()
    expect(screen.queryByRole('menuitemradio', { checked: true })).toBeNull()
  })
})

describe('前台请求走 adminFetch', () => {
  it('写请求带上 X-CSRF-Token 与会话 Cookie', async () => {
    // 后端两个前台 router 上都挂着 Depends(deps.require_csrf)（见
    // app/api/agent_routes.py 与 app/api/session_routes.py）：写方法不带这个
    // 头会被 403。裸 fetch 也不会带上 credentials，跨不过会话 Cookie 那一关。
    const fetchMock = stubApi({
      whoami: { username: 'alice', role: 'member', tenant_id: 'demo', current_tenant_id: 'demo' },
    })
    renderAt('/')
    const user = userEvent.setup()

    await user.type(await screen.findByPlaceholderText(/输入你的问题/), '网络连不上怎么办')
    await user.click(screen.getByRole('button', { name: '发送' }))

    const call = await waitFor(() => {
      const hit = fetchMock.mock.calls.find(([input]) => String(input) === '/agent/chat')
      if (!hit) throw new Error('没有发出 /agent/chat 请求')
      return hit
    })
    const init = call[1] as RequestInit
    expect(new Headers(init.headers).get('X-CSRF-Token')).toBe('token-abc')
    expect(init.credentials).toBe('include')
    // 身份不再由客户端自报：租户和用户都从服务端会话取。
    expect(JSON.parse(String(init.body))).not.toHaveProperty('tenant_id')
    expect(JSON.parse(String(init.body))).not.toHaveProperty('user_id')
  })

  it('前台请求 401 时清掉本地状态、落回登录表单', async () => {
    // 会话是进程内的，后端一重启就全员失效。这时必须回登录页，不能停在
    // 「显示已登录但什么都点不动」的问答界面。
    stubApi({
      whoami: { username: 'alice', role: 'member', tenant_id: 'demo', current_tenant_id: 'demo' },
      sessionsStatus: 401,
    })
    renderAt('/')

    expect(await screen.findByLabelText(/用户名/)).toBeTruthy()
    expect(screen.queryByPlaceholderText(/输入你的问题/)).toBeNull()
  })
})

describe('在前台登录', () => {
  it('登录成功后就地进入问答页，不被弹去后台', async () => {
    // 登录门渲染的是 LoginPage 本体，不是 <Navigate to="/admin/login">：
    // 把人从 `/` 弹到后台地址上，登录完他还得自己走回来，而「不用走回来」
    // 正是这次设计要的。LoginPage 自己在 authenticated 时有一句
    // <Navigate to="/admin" />，它在这里不生效只是因为 ChatRoute 同一次
    // 渲染就把子元素换掉了——这条用例把那个结果钉住。
    let loggedIn = false
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input)
        if (url.includes('/auth/login')) {
          loggedIn = true
          return Promise.resolve(new Response('{}', { status: 200 }))
        }
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
              : new Response('{}', { status: 401 }),
          )
        }
        if (url.includes('/api/admin/tenants')) {
          return Promise.resolve(
            new Response(
              JSON.stringify({ tenants: [{ tenant_id: 'demo', name: 'demo', status: 'active' }] }),
              { status: 200 },
            ),
          )
        }
        if (url.includes('/agent/sessions')) {
          return Promise.resolve(new Response(JSON.stringify({ sessions: [] }), { status: 200 }))
        }
        return new Promise<Response>(() => {})
      }),
    )
    renderAt('/')
    const user = userEvent.setup()

    await user.type(await screen.findByLabelText(/用户名/), 'alice')
    await user.type(screen.getByLabelText(/密码/), 'pw')
    await user.click(screen.getByRole('button', { name: '登录' }))

    expect(await screen.findByTestId('site-topbar')).toBeTruthy()
    expect(screen.getByTestId('pathname').textContent).toBe('/')
  })
})
