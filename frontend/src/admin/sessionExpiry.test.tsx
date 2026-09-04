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

function stubApi(options: { whoami: WhoAmI | 401; switchTenant?: number }): RecordedRequest[] {
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
        return options.whoami === 401 ? json({ detail: '未登录' }, 401) : json(options.whoami)
      }
      if (url.includes('/auth/session/tenant')) {
        const status = options.switchTenant ?? 200
        switched = init?.body === undefined ? switched : String(init.body)
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
  it('会话在服务端已失效时，清掉本地状态跳登录页', async () => {
    // 会话是进程内的，后端一重启所有人都要重新登录。Cookie 还在、界面看起来
    // 像登录着，服务端却已不认——不处理的话用户会卡在一个「显示已登录但什么
    // 都点不动」的界面里。
    signInWithCookie()
    stubApi({ whoami: 401 })
    renderAt(ADMIN_ROUTES.ontology)
    expect(await screen.findByLabelText(/用户名/)).toBeTruthy()
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

  it('admin 的当前租户还是空的时候，进后台就替它定下来', async () => {
    // admin 的 tenant_id 恒为 null，当前租户要显式切过一次才有值。界面上
    // 却总得显示一个租户——不把它同步给服务端的话，界面显示的和服务端
    // 生效的就是两回事（前台那侧会直接 400「请先选择一个租户」）。
    signInWithCookie()
    const requests = stubApi({
      whoami: { username: 'admin', role: 'admin', tenant_id: null, current_tenant_id: null },
    })
    renderAt(ADMIN_ROUTES.ontology)
    await waitFor(() =>
      expect(requests.some((r) => r.url.endsWith('/session/tenant') && r.method === 'PUT')).toBe(true),
    )
    expect(switched).toBe(JSON.stringify({ tenant_id: 'demo' }))
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
    expect(await screen.findByRole('status')).toBeTruthy()
    expect(await screen.findByRole('button', { name: /当前 demo/ })).toBeTruthy()
  })
})
