import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
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
 * 侧边栏徽标。
 *
 * 待审的东西不会自己冒出来告诉你它在等——不点进去就不知道有没有。徽标
 * 把"有多少件事等着你"提到导航上，收起的组也能看见。
 */

interface Badges {
  pending_relations: number
  pending_duplicates: number
  total_terms: number
}

function stubFetch(badges: Badges | 'error') {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/auth/whoami')) return whoamiResponse()
      if (!url.includes('/nav-badges')) return new Promise(() => {})
      if (badges === 'error') return Promise.reject(new Error('boom'))
      return Promise.resolve(new Response(JSON.stringify(badges), { status: 200 }))
    }),
  )
}

beforeEach(() => {
  resetAdminSession()
  localStorage.clear()
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

const nav = () => within(screen.getByRole('navigation', { name: '后台导航' }))

describe('徽标', () => {
  it('待办数显示在对应的链接上', async () => {
    stubFetch({ pending_relations: 7, pending_duplicates: 3, total_terms: 20017 })
    await renderAt(ADMIN_ROUTES.reviewRelations)
    await waitFor(() => {
      expect(nav().getByLabelText('待审关系：7 项待处理')).toBeTruthy()
    })
    expect(nav().getByLabelText('疑似重复：3 项待处理')).toBeTruthy()
  })

  it('收起的组也带着数字——不展开就看不到待办，等于没提醒', async () => {
    stubFetch({ pending_relations: 7, pending_duplicates: 3, total_terms: 20017 })
    await renderAt(ADMIN_ROUTES.documents)
    // 审核组此时是收起的。
    expect(nav().getByRole('button', { name: /审核/ }).getAttribute('aria-expanded')).toBe('false')
    await waitFor(() => {
      expect(nav().getByLabelText('审核：10 项待处理')).toBeTruthy()
    })
  })

  it('零不显示——每个链接后面挂个 0 只是噪音', async () => {
    stubFetch({ pending_relations: 0, pending_duplicates: 4, total_terms: 20017 })
    await renderAt(ADMIN_ROUTES.reviewRelations)
    await waitFor(() => {
      expect(nav().getByLabelText('疑似重复：4 项待处理')).toBeTruthy()
    })
    expect(nav().queryByLabelText(/待审关系/)).toBeNull()
  })

  it('拉不到就不显示，不显示成 0', async () => {
    // 显示 0 是在说"没有待办"，那是一句可能不实的断言。数字拉不到时，
    // 沉默比编一个数好。
    stubFetch('error')
    await renderAt(ADMIN_ROUTES.reviewRelations)
    await waitFor(() => expect(nav().getByRole('link', { name: /待审关系/ })).toBeTruthy())
    expect(nav().queryByLabelText(/项待处理/)).toBeNull()
  })
})

describe('实体总数', () => {
  it('显示在实体列表上，但不是待办徽标', async () => {
    // 「有 20017 条实体」不是一件等着你处理的事。跟待办用同样的样式会
    // 稀释审核那两个数字的意义——那才是真的有事等着你。
    stubFetch({ pending_relations: 7, pending_duplicates: 3, total_terms: 20017 })
    await renderAt(ADMIN_ROUTES.documents)
    await waitFor(() => expect(nav().getByLabelText('实体列表：共 20,017 条')).toBeTruthy())
    expect(nav().queryByLabelText(/实体列表：.*待处理/)).toBeNull()
  })

  it('不算进任何组的待办合计里', async () => {
    // 它不在任何组里，本来就不该被算进去；这条防的是以后有人把独立项
    // 也塞进某个组时顺手把计数一起并了。
    stubFetch({ pending_relations: 7, pending_duplicates: 3, total_terms: 20017 })
    await renderAt(ADMIN_ROUTES.documents)
    await waitFor(() => expect(nav().getByLabelText('审核：10 项待处理')).toBeTruthy())
  })

  it('零条实体不显示——空租户不需要被提醒它是空的', async () => {
    stubFetch({ pending_relations: 0, pending_duplicates: 0, total_terms: 0 })
    await renderAt(ADMIN_ROUTES.documents)
    await waitFor(() => expect(nav().getByRole('link', { name: /实体列表/ })).toBeTruthy())
    expect(nav().queryByLabelText(/共 .* 条/)).toBeNull()
  })
})
