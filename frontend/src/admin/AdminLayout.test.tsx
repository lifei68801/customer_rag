import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
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
 * 侧边栏的行为。
 *
 * 七条链接平铺是上一步的应急做法——它解决了"看不到"，但七条并列读不出
 * 先后。这里按工作阶段分四组，并让当前所在的组自动展开：用户不必记得
 * 自己在哪一段流程里，侧边栏替他标出来。
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

// 404 页也会列出同样的分组名，断言必须限定在导航里。
const nav = () => within(screen.getByRole('navigation', { name: '后台导航' }))

describe('分组', () => {
  it('三个阶段都在，顺序即依赖顺序', async () => {
    await renderAt(ADMIN_ROUTES.documents)
    const headers = nav()
      .getAllByRole('button', { expanded: undefined })
      .map((b) => b.textContent?.trim())
    expect(headers).toEqual(['建模', '接入数据', '审核'])
  })

  it('流程外的实体列表在分组之外，始终可见', async () => {
    // 它不归任何组，所以不该被折叠——每一步之后都可能用到。
    await renderAt(ADMIN_ROUTES.documents)
    expect(nav().getByRole('link', { name: '实体列表' })).toBeTruthy()
  })

  it('当前所在的组自动展开，其余收起', async () => {
    await renderAt(ADMIN_ROUTES.reviewDuplicates)
    expect(nav().getByRole('button', { name: '审核' }).getAttribute('aria-expanded')).toBe('true')
    expect(nav().getByRole('button', { name: '建模' }).getAttribute('aria-expanded')).toBe('false')
    // 展开的组里能看到叶子，收起的组里看不到。
    expect(nav().getByRole('link', { name: '疑似重复' })).toBeTruthy()
    expect(nav().queryByRole('link', { name: '本体图' })).toBeNull()
  })

  it('404 页上不会有任何组被自动展开', async () => {
    // 高亮一个用户并不在的组，比不高亮更糟——他会以为自己在那儿。
    await renderAt('/admin/乱敲')
    for (const label of ['建模', '接入数据', '审核']) {
      expect(nav().getByRole('button', { name: label }).getAttribute('aria-expanded')).toBe('false')
    }
  })
})

describe('折叠状态', () => {
  it('手动展开的组在下次进入时仍然是展开的', async () => {
    const user = userEvent.setup()
    const { unmount } = await renderAt(ADMIN_ROUTES.documents)
    await user.click(nav().getByRole('button', { name: '建模' }))
    expect(nav().getByRole('button', { name: '建模' }).getAttribute('aria-expanded')).toBe('true')
    unmount()

    await renderAt(ADMIN_ROUTES.documents)
    expect(nav().getByRole('button', { name: '建模' }).getAttribute('aria-expanded')).toBe('true')
  })

  it('当前所在的组即使被记成收起，也仍然展开', async () => {
    // 记忆不能盖过"你现在在这儿"——否则用户会看到自己所在的组是收起的，
    // 当前页面在导航上无处对应。
    const user = userEvent.setup()
    const { unmount } = await renderAt(ADMIN_ROUTES.documents)
    await user.click(nav().getByRole('button', { name: '接入数据' }))
    expect(nav().getByRole('button', { name: '接入数据' }).getAttribute('aria-expanded')).toBe('false')
    unmount()

    await renderAt(ADMIN_ROUTES.etl)
    expect(nav().getByRole('button', { name: '接入数据' }).getAttribute('aria-expanded')).toBe('true')
  })

  it('localStorage 读不出来时不报错，退回默认展开规则', async () => {
    localStorage.setItem('admin_nav_collapsed', '不是 JSON')
    await renderAt(ADMIN_ROUTES.etl)
    expect(nav().getByRole('button', { name: '接入数据' }).getAttribute('aria-expanded')).toBe('true')
  })
})

describe('租户', () => {
  it('当前租户名常驻显示在左下角', async () => {
    // 它从顶部搬到了左下角的账号菜单里，但**名字始终可见**——这是把切换
    // 动作收进菜单的前提。看不到当前租户的话，用户会在错的租户里导一批
    // 数据，而那个错误不可撤销。
    await renderAt(ADMIN_ROUTES.documents)
    const aside = screen.getByRole('complementary')
    expect(within(aside).getByRole('button', { name: /账号与租户/ })).toBeTruthy()
  })
})
