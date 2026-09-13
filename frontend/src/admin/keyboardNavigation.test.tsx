import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { ADMIN_ROUTES } from '../adminRoutes'
import { resetAdminSession } from './useAdminAuth'

/**
 * 用键盘穿过后台。
 *
 * 侧边栏有十六条链接。没有跳转链接、换页也不移动焦点的话，每进一个页面都
 * 要从刚点过的那条开始、重新 Tab 穿过剩下的十几条才够得着内容——而 SPA
 * 换页时浏览器本来就不动焦点，读屏软件也不会播报"页面变了"。
 */

function whoamiResponse() {
  return Promise.resolve(
    new Response(
      JSON.stringify({
        username: 'alice',
        role: 'admin',
        tenant_id: 'demo',
        current_tenant_id: 'demo',
      }),
      { status: 200 },
    ),
  )
}

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

async function renderAt(path: string) {
  render(
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
}

const nav = () => within(screen.getByRole('navigation', { name: '后台导航' }))

describe('跳到主内容', () => {
  it('是页面上第一个可聚焦的元素', async () => {
    // 排在导航后面就失去意义了——那时用户已经穿过导航了。
    const user = userEvent.setup()
    await renderAt(ADMIN_ROUTES.documents)

    await user.tab()

    expect(document.activeElement?.textContent).toBe('跳到主内容')
  })

  it('指向主内容区', async () => {
    await renderAt(ADMIN_ROUTES.documents)

    const link = screen.getByRole('link', { name: '跳到主内容' })
    expect(link.getAttribute('href')).toBe('#main-content')
    expect(document.getElementById('main-content')).toBeTruthy()
  })

  it('平时不占位置，聚焦时才显形', async () => {
    // 一直显示的话，每个用户都要看见一条只为键盘用户存在的链接。
    const user = userEvent.setup()
    await renderAt(ADMIN_ROUTES.documents)

    const link = screen.getByRole('link', { name: '跳到主内容' })
    expect(link.className).toMatch(/\bsr-only\b/)
    // focus: 变体把它放回文档流——这条断言防的是"只写了 sr-only、忘了写
    // 显形规则"，那样它永远看不见，等于没有。
    expect(link.className).toMatch(/focus:not-sr-only/)

    await user.tab()
    expect(document.activeElement).toBe(link)
  })
})

describe('换页之后的焦点', () => {
  it('落到主内容区，不留在刚点过的那条链接上', async () => {
    const user = userEvent.setup()
    await renderAt(ADMIN_ROUTES.documents)

    await user.click(nav().getByRole('link', { name: '关系审核' }))

    await waitFor(() => {
      expect(document.activeElement?.id).toBe('main-content')
    })
  })

  it('主内容区不进 Tab 顺序', async () => {
    // 能被脚本聚焦（tabIndex=-1），但不该让每个用键盘的人每页多停一次。
    await renderAt(ADMIN_ROUTES.documents)

    expect(document.getElementById('main-content')?.getAttribute('tabindex')).toBe('-1')
  })

  it('刚进来时不抢焦点', async () => {
    // 首次挂载就抢的话，会打断用户自己正在做的事（比如他刚要点账号菜单）。
    await renderAt(ADMIN_ROUTES.documents)

    expect(document.activeElement?.id).not.toBe('main-content')
  })
})
