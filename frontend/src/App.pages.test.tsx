import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import App from './App'
import { SkinProvider } from './admin/SkinContext'
import { ConfirmProvider } from './admin/ConfirmContext'
import { ToastProvider } from './admin/ToastContext'
import { ADMIN_ROUTES } from './adminRoutes'
import { resetAdminSession } from './admin/useAdminAuth'

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
 * 两个此前被埋起来的页面，现在必须是**自己**，不是别人的一个 tab。
 *
 * 路由接线（App.routing.test.tsx）只保证地址可达；地址可达但渲染出整个
 * 宿主页面，用户看到的仍然是一堆无关的 tab，还得再点一次才看到想看的
 * 东西。这里断言宿主页面的 tab 条不再出现。
 */

beforeEach(() => {
  resetAdminSession()
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

describe('本体图是独立页面', () => {
  it('不再带着本体页的三条 tab', async () => {
    await renderAt(ADMIN_ROUTES.ontologyGraph)
    for (const tab of ['实体类型', '关系类型']) {
      expect(screen.queryByRole('button', { name: tab }), `残留了「${tab}」tab`).toBeNull()
    }
  })

  it('渲染的是图页面本身', async () => {
    // 正向断言。只查"宿主页的 tab 不见了"会假绿：数据 pending 时宿主页的
    // 很多东西本来就还没渲染出来。
    await renderAt(ADMIN_ROUTES.ontologyGraph)
    expect(screen.getByTestId('ontology-graph-page')).toBeTruthy()
  })
})

describe('疑似重复是独立页面', () => {
  it('不再带着审核页的三条 tab', async () => {
    await renderAt(ADMIN_ROUTES.reviewDuplicates)
    for (const tab of ['待审核', '历史记录', '疑似重复术语']) {
      expect(screen.queryByRole('button', { name: tab }), `残留了「${tab}」tab`).toBeNull()
    }
  })

  it('渲染的是重复页面本身', async () => {
    await renderAt(ADMIN_ROUTES.reviewDuplicates)
    expect(screen.getByTestId('duplicates-page')).toBeTruthy()
  })
})

describe('宿主页面不再留重复入口', () => {
  // 页面独立之后，宿主里的旧 tab 就是同一个东西的第二个入口。两个入口
  // 意味着两处状态、两处要改——而且用户在 tab 里点开的那份没有自己的
  // URL，分享不出去。
  it('待审关系页不再有「疑似重复」tab', async () => {
    await renderAt(ADMIN_ROUTES.reviewRelations)
    expect(screen.queryByRole('button', { name: '疑似重复术语' })).toBeNull()
  })
})

describe('宿主页面自己仍然完整', () => {
  it('本体页保留三条 tab', async () => {
    await renderAt(ADMIN_ROUTES.ontology)
    expect(screen.getByRole('button', { name: '实体类型' })).toBeTruthy()
  })

  it('待审关系页保留 tab 条', async () => {
    await renderAt(ADMIN_ROUTES.reviewRelations)
    expect(screen.getByRole('button', { name: '待审核' })).toBeTruthy()
  })
})
