import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'

/**
 * 实体详情页。
 *
 * 存在的理由是**关系**：一个实体有没有用，取决于它连着谁。这在列表行里
 * 放不下，而它正是 GraphRAG 的核心——孤立实体占着存储却从不被命中。
 *
 * 独立 URL 也是刚需：问答诊断页要能直接链过来，同事之间要能发链接。
 */

const NODE_KEY = '公司:可口可乐'
const PATH = `/admin/terms/${encodeURIComponent(NODE_KEY)}`

function stubDetail(body: unknown, status = 200) {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/terms/')) {
        return Promise.resolve(new Response(JSON.stringify(body), { status }))
      }
      return new Promise(() => {})
    }),
  )
}

const term = (over: Record<string, unknown> = {}) => ({
  node_key: NODE_KEY,
  standard_name: '可口可乐',
  aliases: ['Coca-Cola'],
  term_type: '公司',
  extra_properties: { sku: 'A1' },
  source: 'etl',
  relations: [
    { direction: 'out', relation_type: '生产', node_key: '产品:雪碧', standard_name: '雪碧', term_type: '产品' },
    { direction: 'in', relation_type: '隶属', node_key: '类目:饮料', standard_name: '饮料', term_type: '类目' },
  ],
  ...over,
})

beforeEach(() => {
  sessionStorage.setItem('admin_session_token', 'test-token')
  sessionStorage.setItem('admin_current_tenant', 'demo')
  localStorage.clear()
})

function renderAt(path = PATH) {
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

const page = () => within(screen.getByTestId('term-detail'))

describe('基本信息', () => {
  it('标准名、类型、别名、属性都在', async () => {
    stubDetail(term())
    renderAt()
    await waitFor(() => expect(screen.getByTestId('term-detail')).toBeTruthy())
    expect(page().getByRole('heading', { level: 1 }).textContent).toContain('可口可乐')
    expect(page().getByText('Coca-Cola')).toBeTruthy()
    expect(page().getByText('sku')).toBeTruthy()
    expect(page().getByText('A1')).toBeTruthy()
  })

  it('node_key 里的冒号和中文能过 URL', async () => {
    stubDetail(term())
    renderAt()
    await waitFor(() => expect(screen.getByTestId('term-detail')).toBeTruthy())
    // ETL 产出的 node_key 全长这样，路径参数处理不当会让详情页对它们全部打不开。
    // 侧边栏徽标也会发请求，得挑出详情那一条。
    const urls = vi.mocked(fetch).mock.calls.map((c) => decodeURIComponent(String(c[0])))
    expect(urls.some((u) => u.includes(`/terms/${NODE_KEY}`))).toBe(true)
  })
})

describe('关系', () => {
  it('分出方向，每个邻居可点击跳过去', async () => {
    // 「公司 生产 产品」和「产品 生产 公司」是两回事，混在一起看不出这个
    // 实体在关系里扮演什么角色。
    stubDetail(term())
    renderAt()
    await waitFor(() => expect(screen.getByTestId('term-detail')).toBeTruthy())
    const out = page().getByRole('link', { name: /雪碧/ })
    expect(out.getAttribute('href')).toContain(encodeURIComponent('产品:雪碧'))
    expect(page().getByRole('link', { name: /饮料/ })).toBeTruthy()
  })

  it('没有关系时明说这个实体是孤立的', async () => {
    // 孤立实体对检索基本无用——它占着存储却从不被命中。这是个结论，
    // 不是「暂无数据」。
    stubDetail(term({ relations: [] }))
    renderAt()
    await waitFor(() => expect(screen.getByTestId('term-detail')).toBeTruthy())
    expect(page().getByText('这个实体是孤立的，没有任何关系')).toBeTruthy()
  })

  it('关系拉取失败要跟「确实没有关系」区分开', async () => {
    // 后端用 null 表示拉取失败、[] 表示确实没有。混为一谈的话，Neo4j 挂
    // 掉时每个实体都会被报成孤立的。
    stubDetail(term({ relations: null }))
    renderAt()
    await waitFor(() => expect(screen.getByTestId('term-detail')).toBeTruthy())
    expect(page().getByText(/读取.*失败|无法读取/)).toBeTruthy()
    expect(page().queryByText(/孤立/)).toBeNull()
  })
})

describe('实体不存在', () => {
  it('给 404 而不是空白页', async () => {
    stubDetail({ detail: '实体不存在' }, 404)
    renderAt()
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
  })
})

describe('从列表进来', () => {
  it('列表里的实体名是通往详情的链接', async () => {
    // 这是详情页的主入口。列表里点不进去的话，唯一到达方式就只剩手敲 URL。
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input)
        // 列表页现在按类型分组，先拉 summary 再按类型取实体。
        if (url.includes('/terms/summary')) {
          return Promise.resolve(
            new Response(JSON.stringify({ groups: [{ term_type: '公司', total: 1 }] }), {
              status: 200,
            }),
          )
        }
        if (url.includes('/terms')) {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                terms: [
                  {
                    node_key: NODE_KEY,
                    standard_name: '可口可乐',
                    aliases: [],
                    term_type: '公司',
                    extra_properties: {},
                    source: 'etl',
                  },
                ],
                total: 1,
              }),
              { status: 200 },
            ),
          )
        }
        return new Promise(() => {})
      }),
    )
    renderAt('/admin/terms')
    const link = await screen.findByRole('link', { name: '可口可乐' })
    expect(link.getAttribute('href')).toBe(`/admin/terms/${encodeURIComponent(NODE_KEY)}`)
  })
})
