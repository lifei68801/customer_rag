import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import App from './App'
import { SkinProvider } from './admin/SkinContext'
import { ConfirmProvider } from './admin/ConfirmContext'
import { ToastProvider } from './admin/ToastContext'
import { ADMIN_ROUTES, LEGACY_REDIRECTS, NAV_GROUPS, NAV_STANDALONE } from './adminRoutes'
import { resetAdminSession } from './admin/useAdminAuth'

/**
 * 身份不再存 sessionStorage（token 在 HttpOnly Cookie 里，JS 读不到，也
 * 塞不进去）：界面从 whoami 拿身份，所以这里要打桩的是 whoami。
 */
function whoamiResponse() {
  return Promise.resolve(
    new Response(
      JSON.stringify({
        username: 'admin',
        role: 'admin',
        tenant_id: null,
        current_tenant_id: 'demo',
      }),
      { status: 200 },
    ),
  )
}

/**
 * 路由接线的集成测试：断言旧书签真的落在新页面上、敲错 URL 真的看到
 * 404 而不是白屏。
 *
 * 上面 adminRoutes.test.ts 断言的是**数据**（映射表指向哪里），这里断言
 * 的是**接线**（App.tsx 有没有真的把那张表用上）。两者都错过的话，一张
 * 正确的映射表配一个没接的路由树，测试全绿而功能不通。
 */

// 页面组件会在挂载时发请求。这里只关心路由落点，把 fetch 打桩成永远
// pending，避免测试去碰真实网络，也避免未处理的 rejection 污染输出。
beforeEach(() => {
  resetAdminSession()
  // 这些用例要测的是切租户/导航的行为，需要管理员身份——member 的租户
  // 是登录时绑定的，切换这个能力对它不存在。
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) =>
      String(input).includes('/auth/whoami') ? whoamiResponse() : new Promise(() => {}),
    ),
  )
})

// 这三个 Provider 挂在 main.tsx 的根节点（站点级能力，前台后台共用），
// 不在 App 内部，所以测试要自己补上。
// 落点探针。断言"没出现 404 文案"是不够的——在路由还没接线时页面上
// 本来就没有那段文案，测试会假绿。这里直接把当前 URL 渲染出来断言。
function LocationProbe() {
  return <span data-testid="pathname">{useLocation().pathname}</span>
}

// 会话状态是异步的（身份从 whoami 读），后台外壳要等它回来才画得出来。
// 不等的话每条断言都会对着一棵空树跑——404 那几条会假绿。
async function renderAt(path: string) {
  const result = render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[path]}>
            <LocationProbe />
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
  await screen.findByTestId('admin-topbar')
  return result
}

describe('旧路径重定向', () => {
  // 每条垫片都要真的走通，不能只在映射表里写对。
  for (const [legacy, target] of Object.entries(LEGACY_REDIRECTS)) {
    it(`${legacy} 落到 ${target}`, async () => {
      await renderAt(legacy)
      // 垫片是 <Navigate>，它在 effect 里改地址——外壳画出来的那一帧还停在
      // 旧路径上。
      await waitFor(() => expect(screen.getByTestId('pathname').textContent).toBe(target))
    })
  }
})

describe('未匹配路径', () => {
  it('敲错的 admin 路径显示 404，而不是白屏', async () => {
    await renderAt('/admin/这个页面不存在的路径')
    expect(screen.getByTestId('not-found')).toBeTruthy()
  })

  it('404 页把每个去处都摊开，不是死胡同', async () => {
    // 空状态的规矩：必须回答"下一步做什么"。404 尤其如此——用户是迷路了，
    // 只告诉他"没找到"等于把他留在原地。
    //
    // 断言的是每个叶子而不是分组名：漏掉流程外的实体列表，等于那个页面
    // 在这里也是藏着的。
    await renderAt('/admin/乱敲')
    const page = within(screen.getByTestId('not-found'))
    for (const item of [...NAV_GROUPS.flatMap((g) => g.items), ...NAV_STANDALONE]) {
      expect(page.getByRole('link', { name: item.label })).toBeTruthy()
    }
  })

  it('新路径本身不会误判成 404', async () => {
    for (const path of Object.values(ADMIN_ROUTES)) {
      const { unmount } = await renderAt(path)
      expect(screen.queryByTestId('not-found'), `${path} 被误判成 404`).toBeNull()
      unmount()
    }
  })
})
