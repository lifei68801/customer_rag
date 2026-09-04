import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { ADMIN_ROUTES } from '../adminRoutes'
import { resetAdminSession } from './useAdminAuth'

/**
 * 会话由服务端说了算。
 *
 * token 不再存 sessionStorage（它按标签页隔离，同一个人开两个标签页会看到
 * 两个不同的"当前租户"），身份和当前租户都从 whoami 读、切租户走服务端。
 */

interface WhoAmI {
  username: string
  role: string
  tenant_id: string | null
  current_tenant_id: string | null
}

const TENANTS = [
  { tenant_id: 'demo', name: 'demo', status: 'active' },
  { tenant_id: 'acme', name: 'acme', status: 'active' },
]

interface RecordedRequest {
  url: string
  method: string
}

let switched: string | null = null

function stubApi(options: {
  whoami: WhoAmI | 401 | 'pending'
  switchTenant?: number | 'network-error'
}): RecordedRequest[] {
  const requests: RecordedRequest[] = []
  switched = null
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      requests.push({ url, method: (init?.method ?? 'GET').toUpperCase() })
      const json = (body: unknown, status = 200) =>
        Promise.resolve(new Response(JSON.stringify(body), { status }))
      if (url.includes('/auth/whoami')) {
        if (options.whoami === 'pending') return new Promise(() => {})
        return options.whoami === 401 ? json({ detail: '未登录' }, 401) : json(options.whoami)
      }
      if (url.includes('/auth/session/tenant')) {
        switched = init?.body === undefined ? switched : String(init.body)
        if (options.switchTenant === 'network-error') return Promise.reject(new Error('断网'))
        const status = options.switchTenant ?? 200
        if (status === 401) return json({ detail: '登录已过期' }, 401)
        return json(status === 200 ? { ok: true } : { detail: '无权访问该租户' }, status)
      }
      if (url.includes('/api/admin/tenants')) {
        return json({ tenants: TENANTS })
      }
      if (url.includes('/nav-badges')) {
        return json({ pending_relations: 0, pending_duplicates: 0, total_terms: 0 })
      }
      // 没匹配上的接口永不 resolve：这些用例只关心会话和租户，别的数据
      // 停在加载中就行，不用为它们编一份假数据。
      return new Promise(() => {})
    }),
  )
  return requests
}

/**
 * 「浏览器里有会话 Cookie」的模拟。会话 Cookie 本身是 HttpOnly、测试里也
 * 写不进去，能写的只有那个刻意不设 HttpOnly 的 CSRF Cookie——写请求要靠
 * 它带上 X-CSRF-Token 头。身份则完全由 whoami 的桩决定。
 */
function signInWithCookie() {
  document.cookie = 'customer_rag_csrf=csrf-token; path=/'
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  // 会话状态是模块级的，跨用例存活。
  resetAdminSession()
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

describe('会话与租户由服务端驱动', () => {
  it('进后台时服务端已不认这个 Cookie，落到登录页', async () => {
    // 会话是进程内的，后端一重启所有人都要重新登录。Cookie 还在、界面看起来
    // 像登录着，服务端却已不认——不处理的话用户会卡在一个「显示已登录但什么
    // 都点不动」的界面里。
    //
    // 这一条走的是冷启动：进后台的第一个 whoami 就被拒。中途失效是另一条路
    // （adminFetch 的 401），下面单测。
    signInWithCookie()
    stubApi({ whoami: 401 })
    renderAt(ADMIN_ROUTES.ontology)
    expect(await screen.findByLabelText(/用户名/)).toBeTruthy()
  })

  it('已经进了后台，中途会话失效（请求 401）时也落到登录页', async () => {
    // 后端重启发生在用户已经在后台里操作的时候：whoami 早就成功过，状态是
    // 「已登录」，接下来的每个请求却都 401。不把这条路接上，用户会一直看着
    // 一个显示已登录、点什么都没反应的界面。
    signInWithCookie()
    stubApi({
      whoami: { username: 'admin', role: 'admin', tenant_id: null, current_tenant_id: 'demo' },
      switchTenant: 401,
    })
    renderAt(ADMIN_ROUTES.ontology)
    // 先真的进到后台里：这条用例要覆盖的是「本来登录着」，不是冷启动。
    expect(await screen.findByTestId('admin-topbar')).toBeTruthy()
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: /demo/ }))
    await user.click(await screen.findByRole('menuitemradio', { name: /acme/ }))
    expect(await screen.findByLabelText(/用户名/)).toBeTruthy()
  })

  it('whoami 还没回来时，既不画后台也不跳登录页', async () => {
    // 会话状态未知时两个方向都是错的：渲染后台会让 Cookie 已失效的人先看到
    // 一屏取不到数的界面再被踢走；跳登录页则把还登录着的人一脚踢出去。
    signInWithCookie()
    stubApi({ whoami: 'pending' })
    renderAt(ADMIN_ROUTES.ontology)
    // 让挂载后的 effect 和微任务都跑完，再断言「两样都没有」——不等的话
    // 断言只是跑在第一帧上，什么实现都能过。
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0))
    })
    expect(screen.queryByTestId('admin-topbar')).toBeNull()
    expect(screen.queryByLabelText(/用户名/)).toBeNull()
  })

  it('切换租户走服务端，不写 sessionStorage', async () => {
    signInWithCookie()
    const requests = stubApi({
      whoami: { username: 'admin', role: 'admin', tenant_id: null, current_tenant_id: 'demo' },
    })
    renderAt(ADMIN_ROUTES.ontology)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: /demo/ }))
    await user.click(await screen.findByRole('menuitemradio', { name: /acme/ }))
    await waitFor(() =>
      expect(requests.some((r) => r.url.endsWith('/session/tenant') && r.method === 'PUT')).toBe(true),
    )
    expect(sessionStorage.getItem('admin_current_tenant')).toBeNull()
  })

  it('admin 的当前租户还是空的时候，不替它定下来', async () => {
    // 曾经是反过来的：进后台就自动切到租户列表的第一个。那条逻辑挑的是
    // 「列表里排第一的」，跟「用户想用哪个」毫无关系，而唯一的告知是账号块
    // 按钮上的租户名变了——用户以为自己在有两万条术语的主数据租户里，实际
    // 在只有两条示例数据的那个里。系统不知道该去哪时就不该替他决定；界面
    // 改为把选择摆出来（AdminLayout 的「请先选择一个租户」，见
    // tenantGate.test.tsx）。
    signInWithCookie()
    const requests = stubApi({
      whoami: { username: 'admin', role: 'admin', tenant_id: null, current_tenant_id: null },
    })
    renderAt(ADMIN_ROUTES.ontology)
    // 先等租户列表真的拉回来——不等的话下面那条会因为压根还没走到那段
    // 逻辑而假绿。
    await waitFor(() => expect(requests.some((r) => r.url.includes('/api/admin/tenants'))).toBe(true))
    await screen.findByText('请先选择一个租户')
    expect(requests.filter((r) => r.url.endsWith('/session/tenant') && r.method === 'PUT')).toEqual([])
    expect(switched).toBeNull()
  })

  it('切换租户失败时界面留在原来的租户上', async () => {
    // 先更新本地再发请求（或者不看结果就更新）的话，请求失败时界面显示的
    // 租户和服务端生效的那个就对不上了，而后续每一次读写都按服务端那个走。
    signInWithCookie()
    const requests = stubApi({
      whoami: { username: 'admin', role: 'admin', tenant_id: null, current_tenant_id: 'demo' },
      switchTenant: 403,
    })
    renderAt(ADMIN_ROUTES.ontology)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: /demo/ }))
    await user.click(await screen.findByRole('menuitemradio', { name: /acme/ }))
    await waitFor(() =>
      expect(requests.some((r) => r.url.endsWith('/session/tenant') && r.method === 'PUT')).toBe(true),
    )
    // 失败要说出来：租户名没变而没有任何提示的话，用户只会以为自己没点中。
    // 断言的是服务端给的那句话，不是随便一条提示。
    expect((await screen.findByRole('status')).textContent).toMatch(/无权访问该租户/)
    expect(await screen.findByRole('button', { name: /当前 demo/ })).toBeTruthy()
  })

  it('切换租户请求根本没发出去（断网）时也要说出来', async () => {
    // 这一路不经过「响应不 ok」那个分支：fetch 直接抛。吞掉异常的话租户名
    // 没变、也没有任何提示，用户只会以为自己没点中。
    signInWithCookie()
    stubApi({
      whoami: { username: 'admin', role: 'admin', tenant_id: null, current_tenant_id: 'demo' },
      switchTenant: 'network-error',
    })
    renderAt(ADMIN_ROUTES.ontology)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: /demo/ }))
    await user.click(await screen.findByRole('menuitemradio', { name: /acme/ }))
    expect((await screen.findByRole('status')).textContent).toMatch(/切换租户失败/)
    expect(await screen.findByRole('button', { name: /当前 demo/ })).toBeTruthy()
  })
})
