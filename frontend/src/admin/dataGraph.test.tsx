import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { ADMIN_ROUTES } from '../adminRoutes'
import { resetAdminSession } from './useAdminAuth'

/**
 * 图谱预览页。
 *
 * **只测 `DataGraphPage`，`NeighborhoodGraph` 被 mock 掉**——jsdom 没有 WebGL，
 * sigma 在测试环境里跑不起来。这个切分不是为了好测：截断提示属于数据层不属于
 * 渲染层，它在 WebGL 不可用时也必须出现。
 */
vi.mock('./dataGraph/NeighborhoodGraph', () => ({
  NeighborhoodGraph: ({ nodes }: { nodes: { node_key: string }[] }) => (
    <div data-testid="neighborhood-graph">{`画了 ${nodes.length} 个点`}</div>
  ),
}))

interface Payload {
  center: string
  nodes: { node_key: string; standard_name: string; term_type: string | null }[]
  edges: { source: string; relation_type: string; target: string }[]
  truncated: boolean
  total_nodes: number
  shown_nodes: number
}

function payload(over: Partial<Payload> = {}): Payload {
  return {
    center: '产品:Beer',
    nodes: [
      { node_key: '产品:Beer', standard_name: 'Beer', term_type: '产品' },
      { node_key: 'SKU:1', standard_name: 'SKU1', term_type: 'SKU' },
    ],
    edges: [{ source: '产品:Beer', relation_type: 'HAS_SKU', target: 'SKU:1' }],
    truncated: false,
    total_nodes: 2,
    shown_nodes: 2,
    ...over,
  }
}

let body: Payload
let status = 200
let requestedKeys: string[] = []

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
      const match = /\/graph-preview\/(.+)$/.exec(url)
      if (match) {
        requestedKeys.push(decodeURIComponent(match[1]))
        return Promise.resolve(
          new Response(
            JSON.stringify(status === 200 ? body : { detail: '实体不在这个租户的术语表里' }),
            { status },
          ),
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
  body = payload()
  status = 200
  requestedKeys = []
  resetAdminSession()
  localStorage.clear()
  stubApi()
})

async function renderPage(path: string = ADMIN_ROUTES.dataGraph) {
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

describe('图谱预览页', () => {
  it('搜一个实体就画出它的邻域', async () => {
    const user = userEvent.setup()
    await renderPage()

    await user.type(await screen.findByRole('textbox', { name: /实体/ }), '产品:Beer')
    await user.click(screen.getByRole('button', { name: '画出来' }))

    await waitFor(() => expect(screen.getByTestId('neighborhood-graph')).toBeTruthy())
    expect(requestedKeys).toEqual(['产品:Beer'])
  })

  it('截断时把「画了 300 / 共 1,013」写在图旁边', async () => {
    // 这是这一页最重要的一句话。没有它，用户会对着一张不完整的图下一个
    // 完整的结论：「这个实体只连了 300 个东西」——而它连着 1013 个。
    body = payload({ truncated: true, total_nodes: 1013, shown_nodes: 300 })
    const user = userEvent.setup()
    await renderPage()

    await user.type(await screen.findByRole('textbox', { name: /实体/ }), '产品:Beer')
    await user.click(screen.getByRole('button', { name: '画出来' }))

    await waitFor(() => expect(screen.getByText(/300\s*\/\s*共\s*1,013/)).toBeTruthy())
  })

  it('没截断时不显示那句话', async () => {
    // 恒显示的话用户很快就不看它了——而它在真的截断时是关键信息。
    const user = userEvent.setup()
    await renderPage()

    await user.type(await screen.findByRole('textbox', { name: /实体/ }), '产品:Beer')
    await user.click(screen.getByRole('button', { name: '画出来' }))

    await waitFor(() => expect(screen.getByTestId('neighborhood-graph')).toBeTruthy())
    expect(screen.queryByText(/共 /)).toBeNull()
  })

  it('搜一个不存在的实体时说清楚，不是画一张空图', async () => {
    // 空图看起来像「这个实体一条关系都没有」——而真相是它根本不存在，
    // 两句话要用户做的事完全不同（改搜索词 vs 去建这个实体）。
    status = 404
    const user = userEvent.setup()
    await renderPage()

    await user.type(await screen.findByRole('textbox', { name: /实体/ }), '产品:没有这个')
    await user.click(screen.getByRole('button', { name: '画出来' }))

    await waitFor(() => expect(screen.getByText(/不在这个租户的术语表里/)).toBeTruthy())
    expect(screen.queryByTestId('neighborhood-graph')).toBeNull()
    // 搜索框要还在：整页换成错误的话，用户连重新搜一个都做不到。
    expect(screen.getByRole('textbox', { name: /实体/ })).toBeTruthy()
  })

  it('从实体明细带着 node_key 跳过来时直接画，不用再搜一遍', async () => {
    // 这一页最自然的入口不是搜索框，是「我正在看这个实体，想看看它连着
    // 什么」。到了还要再输一遍名字的话，那个入口就白给了。
    await renderPage(`${ADMIN_ROUTES.dataGraph}?node_key=${encodeURIComponent('产品:Beer')}`)

    await waitFor(() => expect(screen.getByTestId('neighborhood-graph')).toBeTruthy())
    expect(requestedKeys).toEqual(['产品:Beer'])
    // 搜索框里也要回填，用户才知道现在画的是哪个。
    expect((screen.getByRole('textbox', { name: /实体/ }) as HTMLInputElement).value).toBe(
      '产品:Beer',
    )
  })

  it('还没搜过时说清楚该做什么，不是一片空白', async () => {
    await renderPage()

    await waitFor(() => expect(screen.getByText(/输入一个实体/)).toBeTruthy())
    expect(screen.queryByTestId('neighborhood-graph')).toBeNull()
  })
})
