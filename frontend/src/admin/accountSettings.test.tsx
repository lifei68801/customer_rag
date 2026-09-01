import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { ADMIN_ROUTES, NAV_GROUPS, NAV_STANDALONE } from '../adminRoutes'

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
  sessionStorage.setItem('admin_session_token', 'test-token')
  localStorage.clear()
  vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
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

const aside = () => within(screen.getByRole('complementary'))

describe('设置页', () => {
  it('皮肤和密度都在这里', () => {
    renderAt(ADMIN_ROUTES.settings)
    expect(screen.getByLabelText('切换配色皮肤')).toBeTruthy()
    expect(screen.getByLabelText('切换列表密度')).toBeTruthy()
  })

  it('不出现在工作流导航里——它不是流程的一站', () => {
    const inNav = [
      ...NAV_GROUPS.flatMap((g) => g.items.map((i) => i.path)),
      ...NAV_STANDALONE.map((i) => i.path),
    ]
    expect(inNav).not.toContain(ADMIN_ROUTES.settings)
  })
})

describe('侧边栏', () => {
  it('不再直接摆着皮肤和密度', () => {
    renderAt(ADMIN_ROUTES.documents)
    expect(aside().queryByLabelText('切换配色皮肤')).toBeNull()
    expect(aside().queryByLabelText('切换列表密度')).toBeNull()
  })

  it('租户切换仍然常驻——它是数据作用域，不是偏好', () => {
    renderAt(ADMIN_ROUTES.documents)
    expect(aside().getByLabelText('切换租户')).toBeTruthy()
  })
})

describe('账号菜单', () => {
  it('默认收着', () => {
    renderAt(ADMIN_ROUTES.documents)
    expect(aside().getByRole('button', { name: '账号' }).getAttribute('aria-expanded')).toBe(
      'false',
    )
    expect(screen.queryByRole('menu', { name: '账号' })).toBeNull()
  })

  it('点开有设置、返回前台、登出', async () => {
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.documents)
    await user.click(aside().getByRole('button', { name: '账号' }))
    // 菜单里它们的角色是 menuitem，不是 link/button——role 属性覆盖了
    // 元素的隐含角色，这正是屏幕阅读器听到的。
    const menu = within(screen.getByRole('menu', { name: '账号' }))
    for (const label of ['设置', '返回前台', '登出']) {
      expect(menu.getByRole('menuitem', { name: label })).toBeTruthy()
    }
  })

  it('登出和其他项之间有分隔——它是有代价的误触', async () => {
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.documents)
    await user.click(aside().getByRole('button', { name: '账号' }))
    expect(within(screen.getByRole('menu', { name: '账号' })).getByRole('separator')).toBeTruthy()
  })

  it('Escape 关上', async () => {
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.documents)
    await user.click(aside().getByRole('button', { name: '账号' }))
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('menu', { name: '账号' })).toBeNull()
  })

  it('选完就关——菜单的用途是选一项', async () => {
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.documents)
    await user.click(aside().getByRole('button', { name: '账号' }))
    await user.click(
      within(screen.getByRole('menu', { name: '账号' })).getByRole('menuitem', { name: '设置' }),
    )
    expect(screen.queryByRole('menu', { name: '账号' })).toBeNull()
  })
})
