import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { ADMIN_ROUTES } from '../adminRoutes'

/**
 * 侧边栏徽标。
 *
 * 待审的东西不会自己冒出来告诉你它在等——不点进去就不知道有没有。徽标
 * 把"有多少件事等着你"提到导航上，收起的组也能看见。
 */

function stubFetch(badges: { pending_relations: number; pending_duplicates: number } | 'error') {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (!url.includes('/nav-badges')) return new Promise(() => {})
      if (badges === 'error') return Promise.reject(new Error('boom'))
      return Promise.resolve(new Response(JSON.stringify(badges), { status: 200 }))
    }),
  )
}

beforeEach(() => {
  sessionStorage.setItem('admin_session_token', 'test-token')
  localStorage.clear()
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

const nav = () => within(screen.getByRole('navigation', { name: '后台导航' }))

describe('徽标', () => {
  it('待办数显示在对应的链接上', async () => {
    stubFetch({ pending_relations: 7, pending_duplicates: 3 })
    renderAt(ADMIN_ROUTES.reviewRelations)
    await waitFor(() => {
      expect(nav().getByLabelText('待审关系：7 项待处理')).toBeTruthy()
    })
    expect(nav().getByLabelText('疑似重复：3 项待处理')).toBeTruthy()
  })

  it('收起的组也带着数字——不展开就看不到待办，等于没提醒', async () => {
    stubFetch({ pending_relations: 7, pending_duplicates: 3 })
    renderAt(ADMIN_ROUTES.documents)
    // 审核组此时是收起的。
    expect(nav().getByRole('button', { name: /审核/ }).getAttribute('aria-expanded')).toBe('false')
    await waitFor(() => {
      expect(nav().getByLabelText('审核：10 项待处理')).toBeTruthy()
    })
  })

  it('零不显示——每个链接后面挂个 0 只是噪音', async () => {
    stubFetch({ pending_relations: 0, pending_duplicates: 4 })
    renderAt(ADMIN_ROUTES.reviewRelations)
    await waitFor(() => {
      expect(nav().getByLabelText('疑似重复：4 项待处理')).toBeTruthy()
    })
    expect(nav().queryByLabelText(/待审关系/)).toBeNull()
  })

  it('拉不到就不显示，不显示成 0', async () => {
    // 显示 0 是在说"没有待办"，那是一句可能不实的断言。数字拉不到时，
    // 沉默比编一个数好。
    stubFetch('error')
    renderAt(ADMIN_ROUTES.reviewRelations)
    await waitFor(() => expect(nav().getByRole('link', { name: /待审关系/ })).toBeTruthy())
    expect(nav().queryByLabelText(/项待处理/)).toBeNull()
  })
})
