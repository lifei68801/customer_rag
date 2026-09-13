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
  it('六个模块都在，顺序即用户的工作顺序', async () => {
    // 只看组头。按 aria-expanded 有没有值来筛：组头都有它，而侧边栏里
    // 别的按钮（版本切换、账号菜单）没有。
    await renderAt(ADMIN_ROUTES.documents)
    const headers = nav()
      .getAllByRole('button')
      .filter((b) => b.hasAttribute('aria-expanded'))
      .map((b) => b.textContent?.trim())
    expect(headers).toEqual(['看板', '本体创建', '数据导入', '数据审核', '结果预览', '日志明细'])
  })

  it('默认全部展开——十六个功能一眼都在', async () => {
    // 此前的规则是"只展开当前所在的组"，为的是让六个组读出工作顺序。
    // 代价是：落在看板上的新用户看到六个组标题加一个条目，另外十五个功能
    // 藏在折叠的标题后面，而「数据审核」「结果预览」这种标题说不出里面
    // 具体有什么。用户的原话是"功能藏得比较深，侧边栏都发现不到"。
    //
    // 全展开不是退回"七条链接平铺"那一版：组标题和顺序都还在，只是不再
    // 替用户把内容藏起来。十六项六组一屏放得下，不需要靠折叠省空间。
    await renderAt(ADMIN_ROUTES.reviewDuplicates)
    for (const label of ['看板', '本体创建', '数据导入', '数据审核', '结果预览', '日志明细']) {
      expect(nav().getByRole('button', { name: label }).getAttribute('aria-expanded')).toBe('true')
    }
    // 不在当前组里的叶子也看得见——这正是改这条规则要买到的东西。
    expect(nav().getByRole('link', { name: '疑似重复' })).toBeTruthy()
    expect(nav().getByRole('link', { name: '本体图' })).toBeTruthy()
  })

  it('404 页上没有任何导航项被标成当前位置', async () => {
    // 高亮一个用户并不在的组，比不高亮更糟——他会以为自己在那儿。
    //
    // 分组默认全展开之后，"展开"不再表示"你在这儿"，所以这条改成直接查
    // 当前项标记（aria-current）：那才是说"你在这儿"的那个信号。
    await renderAt('/admin/乱敲')
    expect(nav().queryAllByRole('link', { current: 'page' })).toHaveLength(0)
  })
})


describe('折叠状态', () => {
  it('手动收起的组在下次进入时仍然是收起的', async () => {
    // 记的是"用户自己收起过哪些"。默认全展开之后，值得记住的是他主动做过
    // 的那个减法——不记的话，每次进后台都要重新收一遍。
    const user = userEvent.setup()
    const { unmount } = await renderAt(ADMIN_ROUTES.documents)
    await user.click(nav().getByRole('button', { name: '本体创建' }))
    expect(nav().getByRole('button', { name: '本体创建' }).getAttribute('aria-expanded')).toBe(
      'false',
    )
    unmount()

    await renderAt(ADMIN_ROUTES.documents)
    expect(nav().getByRole('button', { name: '本体创建' }).getAttribute('aria-expanded')).toBe(
      'false',
    )
  })

  it('当前所在的组即使被记成收起，也仍然展开', async () => {
    // 记忆不能盖过"你现在在这儿"——否则用户会看到自己所在的组是收起的，
    // 当前页面在导航上无处对应。
    const user = userEvent.setup()
    const { unmount } = await renderAt(ADMIN_ROUTES.documents)
    await user.click(nav().getByRole('button', { name: '数据导入' }))
    expect(nav().getByRole('button', { name: '数据导入' }).getAttribute('aria-expanded')).toBe('false')
    unmount()

    // 换到「数据导入」组里的一个页面：这时它是当前组，记忆不该盖过它。
    await renderAt(ADMIN_ROUTES.etl)
    expect(nav().getByRole('button', { name: '数据导入' }).getAttribute('aria-expanded')).toBe('true')
  })

  it('localStorage 读不出来时不报错，退回默认展开规则', async () => {
    localStorage.setItem('admin_nav_collapsed', '不是 JSON')
    await renderAt(ADMIN_ROUTES.etl)
    expect(nav().getByRole('button', { name: '数据导入' }).getAttribute('aria-expanded')).toBe('true')
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
