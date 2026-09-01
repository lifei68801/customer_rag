import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { ADMIN_ROUTES } from '../adminRoutes'

/**
 * 实体列表按类型分组。
 *
 * 此前默认第一页永远是按 standard_name 排序的前 50 个订单号——在一个
 * 20000 条订单号 + 17 条维度实体的租户里，那一页对任何任务都没用。
 *
 * 大基数类型（订单号、用户名）是事实型实体，正确性由 ETL 映射规则保证，
 * 全对或全错，逐条看没有收益；小基数（产品 10、类目 4、公司 3）才是维度
 * 实体，人扫一眼就看完了。
 */

const SUMMARY = {
  groups: [
    { term_type: '订单号', total: 10000 },
    { term_type: '用户名', total: 10000 },
    { term_type: '产品', total: 10 },
    { term_type: '公司', total: 3 },
  ],
}

function stubApi(summary = SUMMARY) {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/terms/summary')) {
        return Promise.resolve(new Response(JSON.stringify(summary), { status: 200 }))
      }
      if (url.includes('/terms')) {
        const type = new URL(url, 'http://x').searchParams.get('term_type') ?? '未知'
        const total = summary.groups.find((g) => g.term_type === type)?.total ?? 0
        return Promise.resolve(
          new Response(
            JSON.stringify({
              terms: Array.from({ length: Math.min(total, 20) }, (_, i) => ({
                node_key: `${type}:${i}`,
                standard_name: `${type}-${i}`,
                aliases: [],
                term_type: type,
                extra_properties: {},
                source: 'etl',
              })),
              total,
            }),
            { status: 200 },
          ),
        )
      }
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  sessionStorage.setItem('admin_session_token', 'test-token')
  sessionStorage.setItem('admin_current_tenant', 'demo')
  localStorage.clear()
  stubApi()
})

function renderPage() {
  return render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[ADMIN_ROUTES.terms]}>
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
}

const group = (name: string) =>
  within(screen.getByRole('group', { name: new RegExp(name) }))

describe('分组摘要', () => {
  it('每个类型一行，带条数', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByRole('group', { name: /订单号/ })).toBeTruthy())
    expect(group('订单号').getByText('10,000')).toBeTruthy()
    expect(group('公司').getByText('3')).toBeTruthy()
  })

  it('大类型折叠，小类型直接列出全部', async () => {
    // 3 条公司不需要点一下才看得到；10000 条订单号点开也看不完。
    renderPage()
    await waitFor(() => expect(screen.getByRole('group', { name: /公司/ })).toBeTruthy())
    await waitFor(() => expect(group('公司').getByText('公司-0')).toBeTruthy())
    expect(group('订单号').queryByText('订单号-0')).toBeNull()
  })

  it('大类型点开之后能看到样本', async () => {
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(screen.getByRole('group', { name: /订单号/ })).toBeTruthy())
    await user.click(group('订单号').getByRole('button', { name: /看样本|展开/ }))
    await waitFor(() => expect(group('订单号').getByText('订单号-0')).toBeTruthy())
  })

  it('样本要说明它是样本，不是全部', async () => {
    // 看到 20 条会默认「就这些」。10000 条里的 20 条不说清楚，用户会据此
    // 得出错误结论。
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(screen.getByRole('group', { name: /订单号/ })).toBeTruthy())
    await user.click(group('订单号').getByRole('button', { name: /看样本|展开/ }))
    await waitFor(() => expect(group('订单号').getByText(/样本|共 10,000/)).toBeTruthy())
  })
})

describe('搜索', () => {
  it('搜索时不分组——找一个具体实体时分组只碍事', async () => {
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(screen.getByRole('group', { name: /订单号/ })).toBeTruthy())
    await user.type(screen.getByLabelText(/搜索|按名称/), '可口')
    await waitFor(() => expect(screen.queryByRole('group', { name: /订单号/ })).toBeNull())
  })
})

describe('空租户', () => {
  it('一个类型都没有时给出下一步', async () => {
    stubApi({ groups: [] })
    renderPage()
    await waitFor(() => expect(screen.getByText(/还没有任何实体/)).toBeTruthy())
  })
})
