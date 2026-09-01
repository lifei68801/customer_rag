import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { ADMIN_ROUTES } from '../adminRoutes'

/**
 * 侧边栏的行为。
 *
 * 七条链接平铺是上一步的应急做法——它解决了"看不到"，但七条并列读不出
 * 先后。这里按工作阶段分四组，并让当前所在的组自动展开：用户不必记得
 * 自己在哪一段流程里，侧边栏替他标出来。
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

// 404 页也会列出同样的分组名，断言必须限定在导航里。
const nav = () => within(screen.getByRole('navigation', { name: '后台导航' }))

describe('分组', () => {
  it('三个阶段都在，顺序即依赖顺序', () => {
    renderAt(ADMIN_ROUTES.documents)
    const headers = nav()
      .getAllByRole('button', { expanded: undefined })
      .map((b) => b.textContent?.trim())
    expect(headers).toEqual(['建模', '接入数据', '审核'])
  })

  it('流程外的实体列表在分组之外，始终可见', () => {
    // 它不归任何组，所以不该被折叠——每一步之后都可能用到。
    renderAt(ADMIN_ROUTES.documents)
    expect(nav().getByRole('link', { name: '实体列表' })).toBeTruthy()
  })

  it('当前所在的组自动展开，其余收起', () => {
    renderAt(ADMIN_ROUTES.reviewDuplicates)
    expect(nav().getByRole('button', { name: '审核' }).getAttribute('aria-expanded')).toBe('true')
    expect(nav().getByRole('button', { name: '建模' }).getAttribute('aria-expanded')).toBe('false')
    // 展开的组里能看到叶子，收起的组里看不到。
    expect(nav().getByRole('link', { name: '疑似重复' })).toBeTruthy()
    expect(nav().queryByRole('link', { name: '本体图' })).toBeNull()
  })

  it('404 页上不会有任何组被自动展开', () => {
    // 高亮一个用户并不在的组，比不高亮更糟——他会以为自己在那儿。
    renderAt('/admin/乱敲')
    for (const label of ['建模', '接入数据', '审核']) {
      expect(nav().getByRole('button', { name: label }).getAttribute('aria-expanded')).toBe('false')
    }
  })
})

describe('折叠状态', () => {
  it('手动展开的组在下次进入时仍然是展开的', async () => {
    const user = userEvent.setup()
    const { unmount } = renderAt(ADMIN_ROUTES.documents)
    await user.click(nav().getByRole('button', { name: '建模' }))
    expect(nav().getByRole('button', { name: '建模' }).getAttribute('aria-expanded')).toBe('true')
    unmount()

    renderAt(ADMIN_ROUTES.documents)
    expect(nav().getByRole('button', { name: '建模' }).getAttribute('aria-expanded')).toBe('true')
  })

  it('当前所在的组即使被记成收起，也仍然展开', async () => {
    // 记忆不能盖过"你现在在这儿"——否则用户会看到自己所在的组是收起的，
    // 当前页面在导航上无处对应。
    const user = userEvent.setup()
    const { unmount } = renderAt(ADMIN_ROUTES.documents)
    await user.click(nav().getByRole('button', { name: '接入数据' }))
    expect(nav().getByRole('button', { name: '接入数据' }).getAttribute('aria-expanded')).toBe('false')
    unmount()

    renderAt(ADMIN_ROUTES.etl)
    expect(nav().getByRole('button', { name: '接入数据' }).getAttribute('aria-expanded')).toBe('true')
  })

  it('localStorage 读不出来时不报错，退回默认展开规则', () => {
    localStorage.setItem('admin_nav_collapsed', '不是 JSON')
    renderAt(ADMIN_ROUTES.etl)
    expect(nav().getByRole('button', { name: '接入数据' }).getAttribute('aria-expanded')).toBe('true')
  })
})

describe('租户', () => {
  it('租户切换器排在导航之前', () => {
    // 租户决定后面看到的每一条数据。它排在导航下面的话，用户会先挑页面
    // 再发现自己在错的租户里，得重来一次。
    renderAt(ADMIN_ROUTES.documents)
    const aside = screen.getByRole('complementary')
    const tenant = within(aside).getByLabelText('切换租户')
    const navEl = within(aside).getByRole('navigation', { name: '后台导航' })
    expect(tenant.compareDocumentPosition(navEl) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
})
