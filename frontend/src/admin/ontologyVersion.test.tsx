import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
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
 * 草稿/已确认这个轴。
 *
 * 它此前是每个页面自己的一份 useState：在本体结构页切到「已确认版本」，
 * 跳到本体图又是草稿——同一件事在两个页面上答案不一样，而且这个状态没
 * 有地址，截图发给同事对方打开看到的是另一份数据。
 *
 * 现在它挂在「建模」这一组上，存在 URL 里。
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

function Probe() {
  const { pathname, search } = useLocation()
  return <span data-testid="url">{pathname + search}</span>
}

// 会话状态是异步的（身份从 whoami 读，token 在 HttpOnly Cookie 里 JS 读不
// 到），后台外壳要等 whoami 回来才画得出来。不等的话断言会对着一棵空树跑。
async function renderAt(path: string) {
  const result = render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[path]}>
            <Probe />
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
const url = () => screen.getByTestId('url').textContent

describe('版本轴挂在建模组上', () => {
  it('建模组展开时才出现', async () => {
    await renderAt(ADMIN_ROUTES.ontology)
    expect(nav().getByRole('group', { name: '本体版本' })).toBeTruthy()
  })

  it('不在建模组时不出现——它只对本体结构和本体图有意义', async () => {
    await renderAt(ADMIN_ROUTES.reviewRelations)
    expect(nav().queryByRole('group', { name: '本体版本' })).toBeNull()
  })

  it('切换写进 URL，可以直接分享', async () => {
    const user = userEvent.setup()
    await renderAt(ADMIN_ROUTES.ontology)
    await user.click(nav().getByRole('button', { name: '已确认' }))
    expect(url()).toBe(`${ADMIN_ROUTES.ontology}?version=confirmed`)
  })

  it('URL 带 version 时控件反映它', async () => {
    await renderAt(`${ADMIN_ROUTES.ontology}?version=confirmed`)
    expect(
      nav().getByRole('button', { name: '已确认' }).getAttribute('aria-pressed'),
    ).toBe('true')
  })
})

describe('页面不再各自维护版本', () => {
  it('本体结构页顶部没有第二个版本控件', async () => {
    await renderAt(ADMIN_ROUTES.ontology)
    expect(screen.queryByRole('group', { name: '查看版本' })).toBeNull()
  })

  it('本体图页也没有自己那份', async () => {
    await renderAt(ADMIN_ROUTES.ontologyGraph)
    const page = within(screen.getByTestId('ontology-graph-page'))
    expect(page.queryByRole('group', { name: '本体版本' })).toBeNull()
  })
})

describe('跨页保持', () => {
  it('从本体结构切到本体图，仍然看的是已确认', async () => {
    const user = userEvent.setup()
    await renderAt(`${ADMIN_ROUTES.ontology}?version=confirmed`)
    await user.click(nav().getByRole('link', { name: '本体图' }))
    expect(url()).toBe(`${ADMIN_ROUTES.ontologyGraph}?version=confirmed`)
  })

  it('离开建模组时版本参数不跟着走', async () => {
    // ?version 对审核页没有意义。带着它跑只会让 URL 说谎——看起来那个页面
    // 也有版本概念。
    const user = userEvent.setup()
    await renderAt(`${ADMIN_ROUTES.ontology}?version=confirmed`)
    await user.click(nav().getByRole('button', { name: '审核' }))
    await user.click(nav().getByRole('link', { name: '待审关系' }))
    expect(url()).toBe(ADMIN_ROUTES.reviewRelations)
  })
})
