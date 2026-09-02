import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { ADMIN_ROUTES } from '../adminRoutes'

/**
 * 租户收进左下角的账号菜单。
 *
 * 关键是**当前租户名常驻显示**，只有切换动作收进菜单。我原先反对把租户
 * 藏起来，担心的是用户忘记自己在哪个租户、往错的那个里导数据；名字一直
 * 在屏幕上，那个风险就没了。
 *
 * 用 click 而不是 hover 触发：hover 菜单在触屏上打不开，而且左下角这个
 * 位置容易被路过——用户去点状态栏或滚动条时就会扫过。
 */

function stubApi(
  initial = [
    { tenant_id: 'demo', name: '演示租户', status: 'active' },
    { tenant_id: 'acme', name: 'ACME', status: 'active' },
  ],
) {
  // 建完之后 GET 要能拿到新租户，真实后端就是这样。用固定列表的话，
  // useTenants 的「当前租户不在列表里就自动纠正」会立刻把新建的那个改
  // 回去——那是 stub 失真，不是缺陷。
  const tenants = [...initial]
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/api/admin/tenants')) {
        if (init?.method === 'POST') {
          const body = JSON.parse(String(init.body)) as { tenant_id: string; name: string }
          const created = { ...body, status: 'active' }
          tenants.push(created)
          return Promise.resolve(new Response(JSON.stringify(created), { status: 200 }))
        }
        return Promise.resolve(new Response(JSON.stringify({ tenants }), { status: 200 }))
      }
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  sessionStorage.setItem('admin_session_token', 'test-token')
  // 这些用例要测的是切租户/导航的行为，需要管理员身份——member 的租户
  // 是登录时绑定的，切换这个能力对它不存在。
  sessionStorage.setItem('admin_role', 'admin')
  sessionStorage.setItem('admin_username', 'admin')
  // 切换租户会写进 sessionStorage，不清掉的话测试之间会串。
  sessionStorage.setItem('admin_current_tenant', 'demo')
  localStorage.clear()
  stubApi()
})

function renderPage() {
  return render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[ADMIN_ROUTES.documents]}>
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
}

const trigger = () => screen.getByRole('button', { name: /账号与租户/ })
const menu = () => within(screen.getByRole('menu', { name: '账号与租户' }))

async function open(user: ReturnType<typeof userEvent.setup>) {
  await waitFor(() => expect(trigger().textContent).toMatch(/演示租户/))
  await user.click(trigger())
}

describe('当前租户常驻显示', () => {
  it('收起时就能看到自己在哪个租户', async () => {
    // 这是把切换动作收进菜单的前提。看不到当前租户的话，用户会在错的
    // 租户里导一批数据——那个错误不可撤销。
    renderPage()
    await waitFor(() => expect(trigger().textContent).toMatch(/演示租户/))
  })

  it('用 click 打开，不是 hover', async () => {
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(trigger().textContent).toMatch(/演示租户/))

    await user.hover(trigger())
    expect(screen.queryByRole('menu', { name: '账号与租户' }), 'hover 就弹出来了').toBeNull()

    await user.click(trigger())
    expect(screen.getByRole('menu', { name: '账号与租户' })).toBeTruthy()
  })
})

describe('菜单内容', () => {
  it('列出所有租户，当前的标出来', async () => {
    const user = userEvent.setup()
    renderPage()
    await open(user)
    expect(menu().getByRole('menuitemradio', { name: /演示租户/ }).getAttribute('aria-checked')).toBe('true')
    expect(menu().getByRole('menuitemradio', { name: /ACME/ }).getAttribute('aria-checked')).toBe('false')
  })

  it('设置和登出都在', async () => {
    const user = userEvent.setup()
    renderPage()
    await open(user)
    // 「返回前台」已经搬到顶栏右上角常驻；「新建租户」搬到了租户管理页。
    for (const label of ['设置', '登出']) {
      expect(menu().getByRole('menuitem', { name: label })).toBeTruthy()
    }
  })

  it('登出跟别的项隔开——它是有代价的误触', async () => {
    const user = userEvent.setup()
    renderPage()
    await open(user)
    expect(menu().getAllByRole('separator').length).toBeGreaterThan(0)
  })
})

describe('切换租户', () => {
  it('选一个就切过去，菜单关上', async () => {
    const user = userEvent.setup()
    renderPage()
    await open(user)
    await user.click(menu().getByRole('menuitemradio', { name: /ACME/ }))
    await waitFor(() => expect(trigger().textContent).toMatch(/ACME/))
    expect(screen.queryByRole('menu', { name: '账号与租户' })).toBeNull()
  })
})

describe('新建租户不在这个菜单里', () => {
  it('入口已经搬到租户管理页', async () => {
    // 这个菜单管的是"我是谁、我在哪个租户"以及去处；新建是一次性的管理
    // 动作。同一个动作留两个入口，改起来就得记得改两处。
    //
    // 随之消失的还有"建完自动切过去"——那在这里是对的（在菜单里建租户，
    // 意图就是马上要用它），在租户管理页则不对：那是管理上下文，可能一次
    // 建好几个，自动切走会把人从管理列表弹到别的租户里去。
    const user = userEvent.setup()
    renderPage()
    await open(user)
    expect(menu().queryByRole('menuitem', { name: '新建租户' })).toBeNull()
    expect(menu().queryByLabelText('新租户 ID')).toBeNull()
  })
})

describe('侧边栏顶部', () => {
  it('不再有独立的租户下拉', async () => {
    renderPage()
    await waitFor(() => expect(trigger().textContent).toMatch(/演示租户/))
    expect(screen.queryByLabelText('切换租户')).toBeNull()
  })
})
