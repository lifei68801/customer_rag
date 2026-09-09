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
 * 脏边与孤儿数据页。
 *
 * 这一页的全部价值在于「不锚定某个实体」——脏边此前只在实体详情页里列得
 * 出来，而它们的特点恰恰是没人知道在哪。
 */
interface Edge {
  subject_node_key: string
  subject_standard_name: string
  relation_type: string
  object_node_key: string
  object_standard_name: string
  edge_tenant_id: string | null
  subject_tenant_id: string | null
  object_tenant_id: string | null
}

function edge(over: Partial<Edge> = {}): Edge {
  return {
    subject_node_key: '产品:洗发水',
    subject_standard_name: '洗发水',
    relation_type: 'RELATED_TO',
    object_node_key: '模块:认证',
    object_standard_name: '认证',
    edge_tenant_id: null,
    subject_tenant_id: 'demo',
    object_tenant_id: 'demo',
    ...over,
  }
}

let body: { edges: Edge[]; truncated: boolean; limit: number }
let status = 200

function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/auth/whoami')) {
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
      if (url.includes('/dirty-edges')) {
        return Promise.resolve(
          new Response(JSON.stringify(status === 200 ? body : { detail: '脏边清单没查出来' }), {
            status,
          }),
        )
      }
      if (url.includes('/nav-badges')) {
        return Promise.resolve(new Response(JSON.stringify({}), { status: 200 }))
      }
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  body = { edges: [edge()], truncated: false, limit: 500 }
  status = 200
  resetAdminSession()
  localStorage.clear()
  stubApi()
})

async function renderPage() {
  const result = render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[ADMIN_ROUTES.reviewDirtyEdges]}>
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
  await screen.findByTestId('admin-topbar')
  return result
}

describe('脏边与孤儿数据页', () => {
  it('列出整个租户的脏边，每条说清哪里不对', async () => {
    // 只说"这条边有问题"的话，运维不知道该不该删——他要判断的是"这是脏
    // 数据还是我不了解的正常情况"。
    await renderPage()

    await waitFor(() => expect(screen.getByTestId('dirty-edge')).toBeTruthy())
    const row = within(screen.getByTestId('dirty-edge'))
    expect(row.getByText(/洗发水.*RELATED_TO.*认证/)).toBeTruthy()
    expect(row.getByText(/没有租户标记/)).toBeTruthy()
  })

  it('哪一侧不对就说哪一侧', async () => {
    // 四种脏法要用不同的话说。都说成「租户标记不对」的话，运维每条都得
    // 自己去查是边不对还是对端不对。
    body = {
      edges: [edge({ edge_tenant_id: 'demo', object_tenant_id: '别人的租户' })],
      truncated: false,
      limit: 500,
    }
    await renderPage()

    await waitFor(() => expect(screen.getByTestId('dirty-edge')).toBeTruthy())
    expect(screen.getByText(/对端实体属于别的租户（别人的租户）/)).toBeTruthy()
  })

  it('被截断时说出来', async () => {
    // 默默少列的话，运维会以为脏边只有这么多——他清完就以为干净了。
    body = { edges: [edge()], truncated: true, limit: 500 }
    await renderPage()

    await waitFor(() => expect(screen.getByText(/只列了前 500 条/)).toBeTruthy())
  })

  it('没被截断时不提这一句', async () => {
    // 每次都提的话那句话就没意义了，运维会学会忽略它。
    await renderPage()

    await waitFor(() => expect(screen.getByTestId('dirty-edge')).toBeTruthy())
    expect(screen.queryByText(/只列了前/)).toBeNull()
  })

  it('每条都给一个去处理的入口', async () => {
    // 光看见没用。删除在实体详情页做（那边有影响面预览和批量确认），
    // 这一页负责让人找到它。
    await renderPage()

    await waitFor(() => expect(screen.getByTestId('dirty-edge')).toBeTruthy())
    const link = within(screen.getByTestId('dirty-edge')).getByRole('link', {
      name: /去.*详情页处理/,
    })
    expect(link.getAttribute('href')).toBe(
      `${ADMIN_ROUTES.terms}/${encodeURIComponent('产品:洗发水')}`,
    )
  })

  it('一条都没有时说清是真的干净', async () => {
    body = { edges: [], truncated: false, limit: 500 }
    await renderPage()

    await waitFor(() => expect(screen.getByText('没有脏边')).toBeTruthy())
  })

  it('查不出来时说出来，不是显示成「没有脏边」', async () => {
    // 「没有脏边」是这一页最不该说错的一句话——运维会据此认为数据是干净的。
    status = 503
    await renderPage()

    await waitFor(() => expect(screen.getByText(/脏边清单没查出来/)).toBeTruthy())
    expect(screen.queryByText('没有脏边')).toBeNull()
  })
})
