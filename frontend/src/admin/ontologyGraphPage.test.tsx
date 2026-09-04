import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
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
 * 本体图真的把数据画出来。
 *
 * 之前的页面测试把 fetch 打桩成永远 pending，只断言"渲染的是图页面本身"
 * ——而永远 pending 时页面显示骨架屏，跟"取数逻辑压根没启动"长得一模一
 * 样。图页确实一直是空的：拉数据的 refresh() 留在了调用方，约束表加了
 * 触发的 effect，图页忘了。没有报错，只是一直转圈。
 */

// sigma 要真实的 WebGL 上下文，jsdom 给不了。这里换掉渲染引擎本身——
// 要验证的是"页面把数据交给了图组件"，不是 sigma 画得对不对（那是它自己
// 的测试该管的事）。换成假组件后断言还更准：直接看传进去的数据。
vi.mock('./ontologyGraph/OntologyGraph', () => ({
  OntologyGraph: ({ termTypes, constraints }: { termTypes: string[]; constraints: unknown[] }) => (
    <div data-testid="graph">
      {termTypes.length} 个实体类型，{constraints.length} 条关系
    </div>
  ),
}))

// 真实形状的响应，覆盖图页要用的四个接口。
function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/auth/whoami')) return whoamiResponse()
      const json = (body: unknown) =>
        Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))
      if (url.includes('/checkout')) return json({})
      if (url.includes('/term-types')) {
        return json({
          term_types: [
            { value: '公司', extra_fields: [], standard_name_value_type: 'string' },
            { value: '产品', extra_fields: [], standard_name_value_type: 'string' },
          ],
        })
      }
      if (url.includes('/relation-types')) {
        return json({
          relation_types: [
            { relation_type: '生产', example_phrase: '', description: '', allow_chain_query: false },
          ],
        })
      }
      if (url.includes('/constraints')) {
        return json({
          constraints: [
            { subject_term_type: '公司', relation_type: '生产', object_term_type: '产品' },
          ],
        })
      }
      if (url.includes('/graph-overlay')) return json({ fanout: [], entity_counts: {} })
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

describe('本体图', () => {
  it('数据到位后画出来，不是一直转圈', async () => {
    renderAt(ADMIN_ROUTES.ontologyGraph)
    await waitFor(() => {
      expect(screen.getByTestId('graph').textContent).toBe('2 个实体类型，1 条关系')
    })
  })

  it('拉数据的请求真的发出去了', async () => {
    renderAt(ADMIN_ROUTES.ontologyGraph)
    await waitFor(() => {
      const urls = vi.mocked(fetch).mock.calls.map((c) => String(c[0]))
      expect(urls.some((u) => u.includes('/constraints'))).toBe(true)
      expect(urls.some((u) => u.includes('/term-types'))).toBe(true)
    })
  })
})
