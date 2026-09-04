import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { ADMIN_ROUTES, PAGE_TITLES } from '../adminRoutes'
import { resetAdminSession } from './useAdminAuth'

/**
 * 「系统不知道你想去哪，就别替你选」。
 *
 * admin 的 tenant_id 恒为 null，当前租户要显式切过一次才有值。此前
 * useTenants 会在当前租户为空时自动切到租户列表的第一个——用户从没选过
 * 租户，系统替他选了，唯一的告知方式是账号块按钮上的租户名变了。真实
 * 后果：用户以为自己在有两万条术语的主数据租户里，实际在只有两条示例
 * 数据的 default 里，然后被一条他从没见过的数据挡住了删除操作。
 *
 * 「当前租户不在真实列表里」和「当前所在租户被停用了」是另一回事：那时
 * 系统**知道**该去哪（当前的无效了，换个有效的），不纠正的话界面会显示一
 * 个不存在的租户名而后续所有写操作 404。这两种情况继续自动纠正，但要说
 * 出来。
 */

interface WhoAmI {
  username: string
  role: string
  tenant_id: string | null
  current_tenant_id: string | null
}

interface RecordedRequest {
  url: string
  method: string
  body: string | null
}

const ACTIVE_TENANTS = [
  { tenant_id: 'demo', name: '演示租户', status: 'active' },
  { tenant_id: 'acme', name: 'ACME', status: 'active' },
]

function stubApi(options: {
  whoami: WhoAmI
  tenants?: { tenant_id: string; name: string; status: string }[]
}): RecordedRequest[] {
  const requests: RecordedRequest[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      requests.push({
        url,
        method: (init?.method ?? 'GET').toUpperCase(),
        body: init?.body === undefined ? null : String(init.body),
      })
      const json = (body: unknown, status = 200) =>
        Promise.resolve(new Response(JSON.stringify(body), { status }))
      if (url.includes('/auth/whoami')) return json(options.whoami)
      if (url.includes('/auth/session/tenant')) return json({ ok: true })
      if (url.includes('/api/admin/tenants')) {
        return json({ tenants: options.tenants ?? ACTIVE_TENANTS })
      }
      if (url.includes('/nav-badges')) {
        return json({ pending_relations: 0, pending_duplicates: 0, total_terms: 0 })
      }
      // 其余接口永不 resolve：这些用例只关心租户门，别的数据停在加载中就行。
      return new Promise(() => {})
    }),
  )
  return requests
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  document.cookie = 'customer_rag_csrf=csrf-token; path=/'
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

const switchRequests = (requests: RecordedRequest[]) =>
  requests.filter((r) => r.url.includes('/auth/session/tenant') && r.method === 'PUT')

describe('从没选过租户', () => {
  it('不替用户选一个', async () => {
    // 系统不知道用户想去哪。替他选一个，选中的还是租户列表里排第一的那个
    // ——跟"他想用哪个"毫无关系。
    const requests = stubApi({
      whoami: { username: 'admin', role: 'admin', tenant_id: null, current_tenant_id: null },
    })
    renderAt(ADMIN_ROUTES.documents)

    // 先等租户列表真的拉回来了——不等的话"没有发出切换请求"会因为压根还
    // 没走到那段逻辑而假绿。
    await waitFor(() => expect(requests.some((r) => r.url.includes('/api/admin/tenants'))).toBe(true))
    await screen.findByText('请先选择一个租户')
    expect(switchRequests(requests)).toEqual([])
  })

  it('依赖租户的页面换成空态，而不是拿兜底租户去取数', async () => {
    stubApi({
      whoami: { username: 'admin', role: 'admin', tenant_id: null, current_tenant_id: null },
    })
    renderAt(ADMIN_ROUTES.documents)

    expect(await screen.findByText('请先选择一个租户')).toBeTruthy()
    // 文案要说清"为什么是你来选"——只说"请选一个"会让人以为系统坏了。
    expect(screen.getByText(/可以访问多个租户/)).toBeTruthy()
    expect(screen.queryByRole('heading', { name: PAGE_TITLES.documents })).toBeNull()
  })

  it('侧边栏和账号块照常渲染——纠正它的地方必须够得着', async () => {
    // 租户切换器就在账号块里。把它一起挡住等于没有退路。
    stubApi({
      whoami: { username: 'admin', role: 'admin', tenant_id: null, current_tenant_id: null },
    })
    renderAt(ADMIN_ROUTES.documents)

    await screen.findByText('请先选择一个租户')
    const aside = screen.getByRole('complementary')
    expect(within(aside).getByRole('navigation', { name: '后台导航' })).toBeTruthy()
    expect(within(aside).getByRole('button', { name: /账号与租户/ })).toBeTruthy()
  })

  it('租户管理页照常可用', async () => {
    // 把它也挡住的话 admin 会被锁死：空态叫他去选一个租户，而唯一能新建
    // 或启用租户的页面也盖着同一张空态。
    stubApi({
      whoami: { username: 'admin', role: 'admin', tenant_id: null, current_tenant_id: null },
    })
    renderAt(ADMIN_ROUTES.tenants)

    expect(await screen.findByRole('heading', { name: PAGE_TITLES.tenants })).toBeTruthy()
    expect(screen.queryByText('请先选择一个租户')).toBeNull()
  })

  it('不会谎报"原来的租户不可用"', async () => {
    // 从没选过租户不是"选的那个坏了"。把这两件事说成同一句，用户会去找一
    // 个他根本没选过的租户出了什么问题。
    stubApi({
      whoami: { username: 'admin', role: 'admin', tenant_id: null, current_tenant_id: null },
    })
    renderAt(ADMIN_ROUTES.documents)

    await screen.findByText('请先选择一个租户')
    expect(screen.queryByRole('status')).toBeNull()
  })
})

describe('当前租户已经不可用', () => {
  it('自动换一个有效的，并且说出来', async () => {
    // 这一种系统知道该去哪：当前这个无效了，换个有效的。不纠正的话界面会
    // 显示一个不存在的租户名，而后续所有写操作都 404。
    //
    // 「租户被停用」走的是同一条判断：/api/admin/tenants 只列启用中的，
    // 被停用的租户跟"压根不存在"在这里是同一个形状。
    const requests = stubApi({
      whoami: { username: 'admin', role: 'admin', tenant_id: null, current_tenant_id: 'ghost' },
    })
    renderAt(ADMIN_ROUTES.documents)

    const toast = await screen.findByRole('status')
    expect(toast.textContent).toMatch(/ghost/)
    expect(toast.textContent).toMatch(/演示租户/)
    await waitFor(() =>
      expect(switchRequests(requests).map((r) => r.body)).toEqual([
        JSON.stringify({ tenant_id: 'demo' }),
      ]),
    )
  })

  it('当前租户就在列表里时，什么都不说也什么都不切', async () => {
    const requests = stubApi({
      whoami: { username: 'admin', role: 'admin', tenant_id: null, current_tenant_id: 'demo' },
    })
    renderAt(ADMIN_ROUTES.documents)

    await waitFor(() => expect(requests.some((r) => r.url.includes('/api/admin/tenants'))).toBe(true))
    await screen.findByRole('heading', { name: PAGE_TITLES.documents })
    expect(switchRequests(requests)).toEqual([])
    expect(screen.queryByRole('status')).toBeNull()
  })
})
