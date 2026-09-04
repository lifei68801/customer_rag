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
 * 窄屏下的导航抽屉。
 *
 * 侧边栏现在有租户选择器、四个分组、皮肤/密度开关和两个按钮。宽屏上这
 * 是一列，手机上就是一整屏——用户每次进后台都要先滚过整个导航才看得到
 * 内容。
 *
 * 抽屉的开合是**语义状态**（aria-expanded / data-open），显示与否交给
 * CSS 断点。这里测的是状态机，不是像素。
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

const trigger = () => screen.getByRole('button', { name: '导航菜单' })
const aside = () => screen.getByRole('complementary')

describe('抽屉', () => {
  it('默认是关的', async () => {
    await renderAt(ADMIN_ROUTES.documents)
    expect(trigger().getAttribute('aria-expanded')).toBe('false')
    expect(aside().getAttribute('data-open')).toBe('false')
  })

  it('点一下打开，再点一下关上', async () => {
    const user = userEvent.setup()
    await renderAt(ADMIN_ROUTES.documents)
    await user.click(trigger())
    expect(aside().getAttribute('data-open')).toBe('true')
    await user.click(trigger())
    expect(aside().getAttribute('data-open')).toBe('false')
  })

  it('选了目的地就自动关上', async () => {
    // 抽屉的用途是选一个去处。选完还挡在那儿，等于每次都要多点一下关掉。
    const user = userEvent.setup()
    await renderAt(ADMIN_ROUTES.documents)
    await user.click(trigger())
    await user.click(screen.getByRole('link', { name: /表格导入/ }))
    expect(aside().getAttribute('data-open')).toBe('false')
  })

  it('切分组不关——那还没选完', async () => {
    const user = userEvent.setup()
    await renderAt(ADMIN_ROUTES.documents)
    await user.click(trigger())
    await user.click(screen.getByRole('button', { name: /审核/ }))
    expect(aside().getAttribute('data-open')).toBe('true')
  })

  it('Escape 关上', async () => {
    // 抽屉盖住内容时，键盘用户需要一条不用找关闭按钮的退路。
    const user = userEvent.setup()
    await renderAt(ADMIN_ROUTES.documents)
    await user.click(trigger())
    await user.keyboard('{Escape}')
    expect(aside().getAttribute('data-open')).toBe('false')
  })

  it('从页面内部跳走也关——不只是点导航', async () => {
    // 这条和上一条的区别：上一条可以靠给导航链接挂 onClick 蒙混过关，这条
    // 不行。空状态、⌘K、页面里的引导链接都会换页面，抽屉都该跟着关上。
    const user = userEvent.setup()
    await renderAt('/admin/乱敲')
    await user.click(trigger())
    const notFound = within(screen.getByTestId('not-found'))
    await user.click(notFound.getByRole('link', { name: '文档上传' }))
    expect(aside().getAttribute('data-open')).toBe('false')
  })
})
