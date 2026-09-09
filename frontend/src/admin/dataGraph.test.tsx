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
let graphThrows = false

vi.mock('./dataGraph/NeighborhoodGraph', () => ({
  NeighborhoodGraph: ({
    nodes,
    edges,
  }: {
    nodes: { node_key: string }[]
    edges: unknown[]
  }) => {
    if (graphThrows) {
      // 模拟 WebGL 不可用时 `new Sigma(...)` 抛出来。
      throw new Error('WebGL context creation failed')
    }
    return (
      <div data-testid="neighborhood-graph">{`画了 ${nodes.length} 个点、${edges.length} 条边`}</div>
    )
  },
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
let errorDetail = '实体不在这个租户的术语表里'
let holdGraphPreview: Promise<void> | null = null
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
        // **在请求发出的这一刻**把回包定下来。等到 respond() 才读 body 的话，
        // 被按住的那次慢请求最后返回的是后一次搜索的数据——竞态用例就白测了。
        const snapshot = JSON.stringify(status === 200 ? body : { detail: errorDetail })
        const snapshotStatus = status
        const respond = () => new Response(snapshot, { status: snapshotStatus })
        // holdGraphPreview 用来把响应按住不放，好观察加载中的样子。
        return holdGraphPreview ? holdGraphPreview.then(respond) : Promise.resolve(respond())
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
  errorDetail = '实体不在这个租户的术语表里'
  holdGraphPreview = null
  graphThrows = false
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

    // 断言点数和边数，不只是"图这个元素在"。只断言元素存在的话，
    // 把 nodes/edges 传成空数组的实现照样绿——而线上每个人对着一张空图。
    await waitFor(() =>
      expect(screen.getByTestId('neighborhood-graph').textContent).toBe('画了 2 个点、1 条边'),
    )
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

  it('图渲染失败时说出来并保留搜索框，不是白屏', async () => {
    // WebGL 被禁用的机器（企业镜像里很常见）上 sigma 会抛异常。没有错误
    // 边界的话 React 会卸载整棵树：数据已经取到了、截断提示已经算好了，
    // 全跟着一起消失，连换一个实体重搜的输入框都没了。
    graphThrows = true
    body = payload({ truncated: true, total_nodes: 1013, shown_nodes: 300 })
    const user = userEvent.setup()
    await renderPage()

    await user.type(await screen.findByRole('textbox', { name: /实体/ }), '产品:Beer')
    await user.click(screen.getByRole('button', { name: '画出来' }))

    // 取到的数据不能跟着图一起消失：兜底文案里要说得出总共多少个节点。
    await waitFor(() => expect(screen.getByText(/这张图没画出来.*1,013/)).toBeTruthy())
    // 截断提示在边界外面，也要还在。
    expect(screen.getByText(/300\s*\/\s*共\s*1,013/)).toBeTruthy()
    expect(screen.getByRole('textbox', { name: /实体/ })).toBeTruthy()
  })

  it('取数途中显示骨架，不是白屏', async () => {
    // 后端要几秒才画得出大邻域。这段时间里什么都不显示的话，用户不知道
    // 是在算还是点了没反应，于是反复点「画出来」。
    let release: (() => void) | null = null
    holdGraphPreview = new Promise<void>((resolve) => {
      release = resolve
    })
    const user = userEvent.setup()
    await renderPage()

    await user.type(await screen.findByRole('textbox', { name: /实体/ }), '产品:Beer')
    await user.click(screen.getByRole('button', { name: '画出来' }))

    await waitFor(() => expect(screen.getByTestId('skeleton')).toBeTruthy())
    expect(screen.queryByTestId('neighborhood-graph')).toBeNull()

    release!()
    await waitFor(() => expect(screen.getByTestId('neighborhood-graph')).toBeTruthy())
    expect(screen.queryByTestId('skeleton')).toBeNull()
  })

  it('图谱查不通时透出 503 那句话，不是说这个实体不存在', async () => {
    // 两句话要用户做的事完全相反：404 是「改搜索词或去建这个实体」，
    // 503 是「图谱挂了，等一会儿再来」。压平成一句的话，Neo4j 挂掉时
    // 用户会跑去重建一个本来就存在的实体。
    status = 503
    errorDetail = '图谱没查出来。这不代表这个实体没有关系——请稍后重试，一直失败请看服务端日志。'
    const user = userEvent.setup()
    await renderPage()

    await user.type(await screen.findByRole('textbox', { name: /实体/ }), '产品:Beer')
    await user.click(screen.getByRole('button', { name: '画出来' }))

    await waitFor(() => expect(screen.getByText(/不代表这个实体没有关系/)).toBeTruthy())
    expect(screen.queryByTestId('neighborhood-graph')).toBeNull()
  })

  it('同一个实体再点一次「画出来」会真的重新取数', async () => {
    // 503 的文案就是「请稍后重试」。重试在界面上必须真能做到——URL 没变
    // 就不发请求的实现下，用户原样再点一次什么也不会发生，那句话指的动作
    // 他执行不了。
    status = 503
    errorDetail = '图谱没查出来。这不代表这个实体没有关系——请稍后重试。'
    const user = userEvent.setup()
    await renderPage()

    await user.type(await screen.findByRole('textbox', { name: /实体/ }), '产品:Beer')
    await user.click(screen.getByRole('button', { name: '画出来' }))
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())

    status = 200
    // 原样再点一次「画出来」——用户看到「请稍后重试」时会做的正是这个。
    // 点「重试」按钮的话就绕开了 handleSubmit，测不到那条死键。
    await user.click(screen.getByRole('button', { name: '画出来' }))

    await waitFor(() => expect(screen.getByTestId('neighborhood-graph')).toBeTruthy())
    expect(requestedKeys).toEqual(['产品:Beer', '产品:Beer'])
  })

  it('先发的请求后到时不许盖掉后发的结果', async () => {
    // 搜 A（邻域大、要三秒）等不及，改搜 B（很快回来）。A 的响应后到却
    // 无条件 setData 的话，搜索框和地址栏写着 B，图和截断提示却是 A 的
    // ——而且这个错误不会自我暴露：图上看不出中心节点的 key，用户拿着
    // 一张张冠李戴的图去下结论。
    let releaseSlow: (() => void) | null = null
    const slow = new Promise<void>((resolve) => {
      releaseSlow = resolve
    })
    holdGraphPreview = slow
    body = payload({ center: '产品:Beer', truncated: true, total_nodes: 1013, shown_nodes: 300 })
    const user = userEvent.setup()
    await renderPage()

    const input = await screen.findByRole('textbox', { name: /实体/ })
    await user.type(input, '产品:Beer')
    await user.click(screen.getByRole('button', { name: '画出来' }))

    // 第二次搜索立刻返回。
    holdGraphPreview = null
    body = payload({
      center: 'SKU:1',
      nodes: [{ node_key: 'SKU:1', standard_name: 'SKU1', term_type: 'SKU' }],
      edges: [],
      truncated: false,
      total_nodes: 1,
      shown_nodes: 1,
    })
    await user.clear(input)
    await user.type(input, 'SKU:1')
    await user.click(screen.getByRole('button', { name: '画出来' }))
    await waitFor(() =>
      expect(screen.getByTestId('neighborhood-graph').textContent).toBe('画了 1 个点、0 条边'),
    )

    // 现在放行慢的那次。
    releaseSlow!()
    await new Promise((resolve) => setTimeout(resolve, 50))

    expect(screen.getByTestId('neighborhood-graph').textContent).toBe('画了 1 个点、0 条边')
    // 截断提示也不能是上一次那张图的。
    expect(screen.queryByText(/300\s*\/\s*共\s*1,013/)).toBeNull()
  })

  it('还没搜过时说清楚该做什么，不是一片空白', async () => {
    await renderPage()

    await waitFor(() => expect(screen.getByText(/输入一个实体/)).toBeTruthy())
    expect(screen.queryByTestId('neighborhood-graph')).toBeNull()
  })
})
