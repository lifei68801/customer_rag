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
import { describeTermDeleteImpact } from './termDeleteImpact'

/**
 * 删实体时的「告知 + 确认」。
 *
 * 实体身上的关系边会跟着一起没（图谱侧是 DETACH DELETE），而那可能是几千
 * 条。这组用例钉住的就一件事：**用户在按下确认之前看得见这个代价**。
 *
 * 桩里的分布故意是「1009 + 4 = 1013」——三个数字互不相等：相等的话，「报
 * 总数」和「报最大的那一项」两种实现都能变绿，那是这里最容易写出的假绿。
 */

const EDGE_TOTAL = 1013
const TOP_TYPE_COUNT = 1009

let previewRequests: { url: string; body: unknown }[] = []
let previewStatus = 200
let deleteCalls: string[] = []

const PREVIEW = {
  term_count: 1,
  edge_total: EDGE_TOTAL,
  by_counterpart_type: [
    { term_type: '订单号', edge_count: TOP_TYPE_COUNT },
    { term_type: '类目', edge_count: 4 },
  ],
}

function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/auth/whoami')) {
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
      if (url.includes('/terms/bulk-delete/preview')) {
        previewRequests.push({ url, body: JSON.parse(String(init?.body ?? '{}')) })
        return Promise.resolve(
          new Response(
            JSON.stringify(previewStatus === 200 ? PREVIEW : { detail: '图谱连不上' }),
            { status: previewStatus },
          ),
        )
      }
      if (url.includes('/terms/summary')) {
        return Promise.resolve(
          new Response(JSON.stringify({ groups: [{ term_type: '产品', total: 1 }] }), {
            status: 200,
          }),
        )
      }
      if (url.includes('/term-types')) {
        return Promise.resolve(new Response(JSON.stringify({ term_types: [] }), { status: 200 }))
      }
      if (init?.method === 'DELETE') {
        deleteCalls.push(url)
        return Promise.resolve(new Response(JSON.stringify({ deleted: true }), { status: 200 }))
      }
      if (url.includes('/terms')) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              terms: [
                {
                  node_key: '产品:Beer',
                  standard_name: 'Beer',
                  aliases: [],
                  term_type: '产品',
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
}

beforeEach(() => {
  previewRequests = []
  previewStatus = 200
  deleteCalls = []
  resetAdminSession()
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

async function openDeleteConfirm(user: ReturnType<typeof userEvent.setup>) {
  renderPage()
  await waitFor(() => expect(screen.getByText('Beer')).toBeTruthy())
  await user.click(screen.getByRole('button', { name: /删除/ }))
}

describe('确认框写明连带删掉的关系边', () => {
  it('点删除会先问一次预演，问的是这一条实体', async () => {
    const user = userEvent.setup()
    await openDeleteConfirm(user)

    await waitFor(() => expect(previewRequests).toHaveLength(1))
    expect(previewRequests[0].body).toEqual({ node_keys: ['产品:Beer'] })
  })

  it('确认框里同时写着实体名、连带的边数和主要来源', async () => {
    const user = userEvent.setup()
    await openDeleteConfirm(user)

    const dialog = await screen.findByRole('alertdialog')
    const text = dialog.textContent ?? ''
    expect(text).toContain('Beer')
    // 总数是 1013，不是最大那一项的 1009——报成 1009 的话用户按下确认后
    // 会多没掉 4 条，而他以为自己已经看过全部代价了。
    expect(text).toContain('1,013 条关系边')
    expect(text).toContain('1,009 条来自订单号')
  })

  it('确认之前一条都不许删', async () => {
    const user = userEvent.setup()
    await openDeleteConfirm(user)

    await screen.findByRole('alertdialog')
    expect(deleteCalls).toEqual([])
  })

  it('按下确认才真删', async () => {
    const user = userEvent.setup()
    await openDeleteConfirm(user)

    const dialog = await screen.findByRole('alertdialog')
    await user.click(within(dialog).getByRole('button', { name: '确认' }))

    await waitFor(() => expect(deleteCalls).toHaveLength(1))
    expect(deleteCalls[0]).toContain(encodeURIComponent('产品:Beer'))
  })

  it('预演失败就不弹确认框，也不删——拿不到数字就弹一个不写代价的框，等于退回直接删', async () => {
    previewStatus = 500
    const user = userEvent.setup()
    await openDeleteConfirm(user)

    await waitFor(() => expect(screen.getByText(/图谱连不上/)).toBeTruthy())
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(deleteCalls).toEqual([])
  })
})

describe('describeTermDeleteImpact', () => {
  it('一条边都不牵连时不说话——「同时会删掉 0 条关系边」是句废话', () => {
    expect(
      describeTermDeleteImpact({ term_count: 3, edge_total: 0, by_counterpart_type: [] }),
    ).toBe('')
  })

  it('全部来自同一类时说「全部来自」，不说「其中 X 条来自」', () => {
    // 「其中 7 条来自订单号」在总数也是 7 的时候读起来像还有别的类型没列
    // 出来，用户会去找那个不存在的余数。
    const text = describeTermDeleteImpact({
      term_count: 1,
      edge_total: 7,
      by_counterpart_type: [{ term_type: '订单号', edge_count: 7 }],
    })
    expect(text).toContain('全部来自订单号')
    expect(text).not.toContain('其中')
  })

  it('批量时不说「它的」——一批实体没有「它」', () => {
    const text = describeTermDeleteImpact({
      term_count: 10,
      edge_total: 9847,
      by_counterpart_type: [{ term_type: '订单号', edge_count: 9800 }],
    })
    expect(text).toContain('9,847 条关系边')
    expect(text).not.toContain('它的')
  })
})
