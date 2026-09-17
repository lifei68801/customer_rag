import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { ADMIN_ROUTES, modelingWay } from '../adminRoutes'
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
 * 它此前是每个页面自己的一份 useState：在本体结构页（现在是本体建模的
 * 手动构建 tab）切到「已确认版本」，跳到本体图又是草稿——同一件事在两个
 * 页面上答案不一样，而且这个状态没有地址，截图发给同事对方打开看到的是
 * 另一份数据。
 *
 * 状态存在 URL 里（?version=），控件放在手动构建 tab 和本体图页的页头——
 * 它是"这一页在看哪一版"的上下文，放在几十像素外的侧边栏里没人会去找。
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

describe('版本轴在页头', () => {
  it('本体建模页手动构建 tab 的顶上有它', async () => {
    await renderAt(modelingWay('manual'))
    expect(screen.getByRole('group', { name: '本体版本' })).toBeTruthy()
  })

  it('本体图页也有一个——两个页都要能就地切', async () => {
    await renderAt(ADMIN_ROUTES.ontologyGraph)
    const page = within(screen.getByTestId('ontology-graph-page'))
    expect(page.getByRole('group', { name: '本体版本' })).toBeTruthy()
  })

  it('别的页面没有——这个轴只对手动构建和本体图有意义', async () => {
    await renderAt(ADMIN_ROUTES.reviewRelations)
    expect(screen.queryByRole('group', { name: '本体版本' })).toBeNull()
  })

  it('侧边栏里不再有一份——同一个轴两个控件，用户会以为它们管的不是同一件事', async () => {
    await renderAt(modelingWay('manual'))
    expect(nav().queryByRole('group', { name: '本体版本' })).toBeNull()
  })

  it('切换写进 URL，可以直接分享', async () => {
    const user = userEvent.setup()
    await renderAt(modelingWay('manual'))
    await user.click(screen.getByRole('button', { name: '已确认' }))
    // way 留在前面：切版本不该把"在哪个 tab"抹掉
    expect(url()).toBe(`${modelingWay('manual')}&version=confirmed`)
  })

  it('URL 带 version 时控件反映它', async () => {
    await renderAt(`${modelingWay('manual')}&version=confirmed`)
    expect(
      screen.getByRole('button', { name: '已确认' }).getAttribute('aria-pressed'),
    ).toBe('true')
  })
})

describe('两个页面共用同一份状态', () => {
  it('页面上没有第二个自己的版本控件', async () => {
    await renderAt(modelingWay('manual'))
    // 页内只有这一个「本体版本」组；旧实现里各页还有一份自己的 useState
    expect(screen.queryByRole('group', { name: '查看版本' })).toBeNull()
    expect(screen.getAllByRole('group', { name: '本体版本' })).toHaveLength(1)
  })
})

describe('跨页保持', () => {
  it('从手动构建切到本体图，仍然看的是已确认', async () => {
    const user = userEvent.setup()
    await renderAt(`${modelingWay('manual')}&version=confirmed`)
    await user.click(nav().getByRole('link', { name: '本体图' }))
    // 只带 version，不带 way：哪个构建 tab 是本体建模页自己的事，本体图页
    // 没有这个概念，带过去只会让 URL 说谎。
    expect(url()).toBe(`${ADMIN_ROUTES.ontologyGraph}?version=confirmed`)
  })

  it('离开建模组时版本参数不跟着走', async () => {
    // ?version 对审核页没有意义。带着它跑只会让 URL 说谎——看起来那个页面
    // 也有版本概念。
    //
    // 分组默认全展开，所以不用先点开「数据审核」——点它反而是收起。
    const user = userEvent.setup()
    await renderAt(`${modelingWay('manual')}&version=confirmed`)
    await user.click(nav().getByRole('link', { name: '关系审核' }))
    expect(url()).toBe(ADMIN_ROUTES.reviewRelations)
  })
})
