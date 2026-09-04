import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'
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
        current_tenant_id: TENANT,
      }),
      { status: 200 },
    ),
  )
}

/**
 * 前端请求的路径里必须真的带上租户段。
 *
 * 既有测试的 fetch stub 都用 `url.includes('/nav-badges')` 这类模糊匹配，
 * 新旧路径都能命中——把 URL 改错了它们照样绿，证明不了任何事。这个文件
 * 断言的是**完整路径**，以及旧形状的绝迹。
 */

const TENANT = 'demo'
let calls: string[] = []

function stubApi() {
  calls = []
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/auth/whoami')) return whoamiResponse()
      calls.push(url)
      const json = (body: unknown) =>
        Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))
      if (url.includes('/nav-badges')) {
        return json({ pending_relations: 0, pending_duplicates: 0, total_terms: 0 })
      }
      if (url.includes('/documents')) {
        return json({ documents: [], total: 0, pending_jobs: [], dead_jobs: [] })
      }
      if (url.includes('/graph-reviews')) return json({ reviews: [], total: 0 })
      if (url.includes('/duplicate-reviews')) return json({ suggestions: [], total: 0 })
      if (url.includes('/api/admin/tenants')) {
        return json({ tenants: [{ tenant_id: TENANT, name: '演示租户', status: 'active' }] })
      }
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  resetAdminSession()
  localStorage.clear()
  stubApi()
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

/** 请求过的、命中这个片段的完整 URL。 */
const callsMatching = (fragment: string) => calls.filter((u) => u.includes(fragment))

describe('前端请求带上租户段', () => {
  it('侧边栏徽标走 /api/admin/{tenant}/nav-badges', async () => {
    renderAt(ADMIN_ROUTES.documents)
    await waitFor(() => expect(callsMatching('/nav-badges').length).toBeGreaterThan(0))
    for (const url of callsMatching('/nav-badges')) {
      expect(url).toContain(`/api/admin/${TENANT}/nav-badges`)
      // 旧形状必须绝迹：查询参数里再带一个 tenant_id 说明改漏了。
      expect(url).not.toContain('nav-badges?tenant_id=')
    }
  })

  it('文档列表走 /api/admin/{tenant}/documents', async () => {
    renderAt(ADMIN_ROUTES.documents)
    await waitFor(() => expect(callsMatching('/documents').length).toBeGreaterThan(0))
    for (const url of callsMatching('/documents')) {
      expect(url).toContain(`/api/admin/${TENANT}/documents`)
      expect(url).not.toContain('tenant_id=')
    }
  })

  it('关系审核走 /api/admin/{tenant}/graph-reviews', async () => {
    renderAt(ADMIN_ROUTES.reviewRelations)
    await waitFor(() => expect(callsMatching('/graph-reviews').length).toBeGreaterThan(0))
    for (const url of callsMatching('/graph-reviews')) {
      expect(url).toContain(`/api/admin/${TENANT}/graph-reviews`)
      expect(url).not.toContain('tenant_id=')
    }
  })

  it('疑似重复走 /api/admin/{tenant}/duplicate-reviews', async () => {
    renderAt(ADMIN_ROUTES.reviewDuplicates)
    await waitFor(() => expect(callsMatching('/duplicate-reviews').length).toBeGreaterThan(0))
    for (const url of callsMatching('/duplicate-reviews')) {
      expect(url).toContain(`/api/admin/${TENANT}/duplicate-reviews`)
      expect(url).not.toContain('tenant_id=')
    }
  })
})
