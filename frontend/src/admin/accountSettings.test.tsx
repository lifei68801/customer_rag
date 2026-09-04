import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { ADMIN_ROUTES, NAV_GROUPS, NAV_STANDALONE } from '../adminRoutes'
import { resetAdminSession } from './useAdminAuth'

/**
 * 身份不再存 sessionStorage（token 在 HttpOnly Cookie 里，JS 读不到，也
 * 塞不进去）：界面从 whoami 拿身份，所以这里要打桩的是 whoami。
 */
function whoamiResponse() {
  return Promise.resolve(
    new Response(
      JSON.stringify({
        username: 'alice',
        role: 'member',
        tenant_id: 'demo',
        current_tenant_id: 'demo',
      }),
      { status: 200 },
    ),
  )
}

/**
 * 账号相关的东西从侧边栏收走，只留工作流程。
 *
 * 皮肤和密度是个人显示偏好——改错了看着不顺眼，改回来即可。它们和「待审
 * 关系」并排放在侧边栏里，占的是工作流的位置。
 *
 * 租户不在收走之列。它是数据作用域，决定你看到的每一条数据、你的写操作
 * 落到哪个租户上；藏进二级页面的代价是往错误的租户里导一批数据，不可
 * 撤销。Foundry 的 project 选择器也一直常驻。
 */

beforeEach(() => {
  resetAdminSession()
  localStorage.clear()
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) =>
      String(input).includes('/auth/whoami') ? whoamiResponse() : new Promise(() => {}),
    ),
  )
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

const aside = () => within(screen.getByRole('complementary'))

describe('设置页', () => {
  it('皮肤和密度都在这里', async () => {
    await renderAt(ADMIN_ROUTES.settings)
    expect(screen.getByLabelText('切换配色皮肤')).toBeTruthy()
    expect(screen.getByLabelText('切换列表密度')).toBeTruthy()
  })

  it('不出现在工作流导航里——它不是流程的一站', async () => {
    const inNav = [
      ...NAV_GROUPS.flatMap((g) => g.items.map((i) => i.path)),
      ...NAV_STANDALONE.map((i) => i.path),
    ]
    expect(inNav).not.toContain(ADMIN_ROUTES.settings)
  })
})

describe('侧边栏', () => {
  it('不再直接摆着皮肤和密度', async () => {
    await renderAt(ADMIN_ROUTES.documents)
    expect(aside().queryByLabelText('切换配色皮肤')).toBeNull()
    expect(aside().queryByLabelText('切换列表密度')).toBeNull()
  })

  it('当前租户名仍然常驻——它是数据作用域，不是偏好', async () => {
    // 切换动作收进了菜单，名字没有。看不到当前租户的代价是往错的租户里
    // 导数据，不可撤销；看不到皮肤设置的代价是多点一下。
    await renderAt(ADMIN_ROUTES.documents)
    expect(aside().getByRole('button', { name: /账号与租户/ })).toBeTruthy()
  })
})

describe('账号菜单', () => {
  it('默认收着', async () => {
    await renderAt(ADMIN_ROUTES.documents)
    expect(aside().getByRole('button', { name: /账号与租户/ }).getAttribute('aria-expanded')).toBe(
      'false',
    )
    expect(screen.queryByRole('menu', { name: '账号与租户' })).toBeNull()
  })

  it('点开有设置和登出', async () => {
    const user = userEvent.setup()
    await renderAt(ADMIN_ROUTES.documents)
    await user.click(aside().getByRole('button', { name: /账号与租户/ }))
    // 菜单里它们的角色是 menuitem，不是 link/button——role 属性覆盖了
    // 元素的隐含角色，这正是屏幕阅读器听到的。
    const menu = within(screen.getByRole('menu', { name: '账号与租户' }))
    // 「返回前台」不在这里——它常驻顶栏右上角（见 adminChrome.test.tsx）。
    for (const label of ['设置', '登出']) {
      expect(menu.getByRole('menuitem', { name: label })).toBeTruthy()
    }
  })

  // 「登出跟别的项隔开」由 tenantMenu.test.tsx 断言——菜单现在有两条
  // 分隔线（租户区一条、登出前一条），那边的断言更贴合当前形态。

  it('Escape 关上', async () => {
    const user = userEvent.setup()
    await renderAt(ADMIN_ROUTES.documents)
    await user.click(aside().getByRole('button', { name: /账号与租户/ }))
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('menu', { name: '账号与租户' })).toBeNull()
  })

  it('选完就关——菜单的用途是选一项', async () => {
    const user = userEvent.setup()
    await renderAt(ADMIN_ROUTES.documents)
    await user.click(aside().getByRole('button', { name: /账号与租户/ }))
    await user.click(
      within(screen.getByRole('menu', { name: '账号与租户' })).getByRole('menuitem', { name: '设置' }),
    )
    expect(screen.queryByRole('menu', { name: '账号与租户' })).toBeNull()
  })
})
