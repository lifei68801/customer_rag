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
import { buildBulkDeleteConfirmMessage } from './bulkDelete'

/**
 * 实体明细的批量删除。
 *
 * 两级全选：表头复选框勾**本页**，勾上之后可以升级成**当前筛选条件下的
 * 全部**。这两档的破坏力差两个数量级，界面上必须一眼可辨，发出去的请求
 * 也是两种互斥的模式。
 *
 * 下面的桩里，**本页 3 条、筛选条件下 7 条**——两个数字故意不同：相等的话
 * 「删本页」和「删全部」两种实现都能让断言变绿，那是这组用例最容易写出的
 * 假绿。
 */

const PAGE_ROWS = 3
const TOTAL_UNDER_FILTER = 7

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

let bulkRequests: { url: string; body: unknown }[] = []
let previewRequests: { url: string; body: unknown }[] = []
let previewResponse = { term_count: 0, edge_total: 0, by_counterpart_type: [] as { term_type: string; edge_count: number }[] }
let bulkResponse = { requested: 3, deleted: 3, failures: [] as { key: string; reason: string }[] }

const SUMMARY = {
  groups: [
    { term_type: '订单号', total: 4712 },
    { term_type: '公司', total: 3 },
  ],
}

function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/auth/whoami')) return whoamiResponse()
      // 预演必须排在真删前面：真删那条 includes 也能命中 /preview，顺序
      // 反过来的话确认框拿到的是一份 BulkDeleteResult，而它没有 edge_total。
      if (url.includes('/terms/bulk-delete/preview')) {
        previewRequests.push({ url, body: JSON.parse(String(init?.body ?? '{}')) })
        return Promise.resolve(new Response(JSON.stringify(previewResponse), { status: 200 }))
      }
      if (url.includes('/terms/bulk-delete')) {
        bulkRequests.push({ url, body: JSON.parse(String(init?.body ?? '{}')) })
        return Promise.resolve(new Response(JSON.stringify(bulkResponse), { status: 200 }))
      }
      if (url.includes('/terms/summary')) {
        return Promise.resolve(new Response(JSON.stringify(SUMMARY), { status: 200 }))
      }
      if (url.includes('/term-types')) {
        return Promise.resolve(new Response(JSON.stringify({ term_types: [] }), { status: 200 }))
      }
      if (url.includes('/terms')) {
        const params = new URL(url, 'http://x').searchParams
        const termType = params.get('term_type')
        const q = params.get('q')
        // 搜索路径：本页 3 条，但筛选条件下共 7 条。两个数字必须不同。
        if (q) {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                terms: Array.from({ length: PAGE_ROWS }, (_, i) => ({
                  node_key: `t:命中${i}`,
                  standard_name: `命中${i}`,
                  aliases: [],
                  term_type: 't',
                  extra_properties: {},
                  source: 'etl',
                })),
                total: TOTAL_UNDER_FILTER,
              }),
              { status: 200 },
            ),
          )
        }
        const type = termType ?? '公司'
        const total = SUMMARY.groups.find((g) => g.term_type === type)?.total ?? 0
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
  bulkRequests = []
  previewRequests = []
  previewResponse = { term_count: 0, edge_total: 0, by_counterpart_type: [] }
  bulkResponse = { requested: 3, deleted: 3, failures: [] }
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

/** 搜出「本页 3 条 / 筛选条件下 7 条」那份平铺列表。 */
async function searchForFlatList(user: ReturnType<typeof userEvent.setup>) {
  renderPage()
  await waitFor(() => expect(screen.getByRole('group', { name: /订单号/ })).toBeTruthy())
  await user.type(screen.getByLabelText(/搜索|按名称/), '命中')
  await waitFor(() => expect(screen.getByText('命中0')).toBeTruthy())
}

async function confirmDialog(user: ReturnType<typeof userEvent.setup>) {
  const dialog = await screen.findByRole('alertdialog')
  const text = dialog.textContent ?? ''
  await user.click(within(dialog).getByRole('button', { name: '确认' }))
  return text
}

describe('两级全选', () => {
  it('表头复选框勾的是本页，提示条说出本页条数', async () => {
    const user = userEvent.setup()
    await searchForFlatList(user)

    await user.click(screen.getByRole('checkbox', { name: /选中本页/ }))

    const notice = await screen.findByTestId('bulk-selection-notice-flat')
    expect(notice.textContent).toContain(`已选中本页 ${PAGE_ROWS} 条`)
  })

  it('勾上本页之后给出升级入口，并写明筛选条件下的真实总条数', async () => {
    // 「本页 3 条」和「筛选条件下 7 条」是两个不同的数字：升级按钮上写的
    // 必须是后者，写成前者的话这个按钮就没有存在的意义了。
    const user = userEvent.setup()
    await searchForFlatList(user)

    await user.click(screen.getByRole('checkbox', { name: /选中本页/ }))

    const escalate = await screen.findByRole('button', {
      name: new RegExp(`改为选中筛选条件下的全部 ${TOTAL_UNDER_FILTER} 条`),
    })
    expect(escalate).toBeTruthy()
  })

  it('升级之后提示条改口说「全部」，跟本页那一档不是同一句话', async () => {
    const user = userEvent.setup()
    await searchForFlatList(user)
    await user.click(screen.getByRole('checkbox', { name: /选中本页/ }))
    await user.click(
      await screen.findByRole('button', { name: /改为选中筛选条件下的全部/ }),
    )

    const notice = await screen.findByTestId('bulk-selection-notice-flat')
    expect(notice.textContent).toContain('全部')
    expect(notice.textContent).toContain(String(TOTAL_UNDER_FILTER))
    // 本页那一档的说法必须消失——两档同时显示等于没区分。
    expect(notice.textContent).not.toContain(`已选中本页 ${PAGE_ROWS} 条`)
  })
})

describe('发出去的请求', () => {
  it('本页全选发的是 node_keys，不是筛选条件', async () => {
    const user = userEvent.setup()
    await searchForFlatList(user)
    await user.click(screen.getByRole('checkbox', { name: /选中本页/ }))
    await user.click(screen.getByRole('button', { name: /删除选中的 3 条/ }))
    await confirmDialog(user)

    await waitFor(() => expect(bulkRequests.length).toBe(1))
    const body = bulkRequests[0].body as { node_keys?: string[]; filters?: unknown }
    expect(body.node_keys).toEqual(['t:命中0', 't:命中1', 't:命中2'])
    // 两种模式互斥，后端同时收到会报 400——前端不能两个都发。
    expect(body.filters).toBeUndefined()
  })

  it('「全部」发的是筛选条件，不是先把 7 条拉回来再逐条删', async () => {
    const user = userEvent.setup()
    await searchForFlatList(user)
    await user.click(screen.getByRole('checkbox', { name: /选中本页/ }))
    await user.click(await screen.findByRole('button', { name: /改为选中筛选条件下的全部/ }))
    await user.click(screen.getByRole('button', { name: /删除选中的/ }))
    await confirmDialog(user)

    await waitFor(() => expect(bulkRequests.length).toBe(1))
    const body = bulkRequests[0].body as { node_keys?: string[]; filters?: { q?: string } }
    expect(body.node_keys).toBeUndefined()
    expect(body.filters?.q).toBe('命中')
  })
})

describe('确认框', () => {
  it('本页 3 条：说清是选中的 3 条', async () => {
    const user = userEvent.setup()
    await searchForFlatList(user)
    await user.click(screen.getByRole('checkbox', { name: /选中本页/ }))
    await user.click(screen.getByRole('button', { name: /删除选中的 3 条/ }))

    const text = await confirmDialog(user)
    expect(text).toContain('确定删除选中的 3 条实体吗？此操作不可撤销。')
  })

  it('「全部」：写出筛选条件和真实条数，不是只问「确定删除全部吗」', async () => {
    const user = userEvent.setup()
    await searchForFlatList(user)
    await user.click(screen.getByRole('checkbox', { name: /选中本页/ }))
    await user.click(await screen.findByRole('button', { name: /改为选中筛选条件下的全部/ }))
    await user.click(screen.getByRole('button', { name: /删除选中的/ }))

    const text = await confirmDialog(user)
    expect(text).toContain(String(TOTAL_UNDER_FILTER))
    expect(text).toContain('搜索「命中」')
  })

  it('一个筛选条件都没有时直说是整租户，不绕成「筛选条件（没有任何筛选）」', () => {
    // 这是破坏力最大的那一档，不该同时是读起来最费劲的那一句。
    const message = buildBulkDeleteConfirmMessage(
      { mode: 'filters', filters: {}, total: 4712 },
      '实体',
    )
    expect(message).toContain('这个租户的全部 4,712 条实体')
    expect(message).not.toContain('筛选条件（')
  })

  it('几千条的文案比几条更重——同一句话会让确认退化成肌肉记忆', () => {
    const few = buildBulkDeleteConfirmMessage(
      { mode: 'filters', filters: { term_type: '订单号' }, total: 7 },
      '实体',
    )
    const many = buildBulkDeleteConfirmMessage(
      { mode: 'filters', filters: { term_type: '订单号' }, total: 4712 },
      '实体',
    )
    expect(few).not.toBe(many)
    expect(many).toContain('4,712')
    expect(many).toContain('大批量')
    expect(few).not.toContain('大批量')
  })
})

describe('结果', () => {
  it('部分失败时逐条列出没删掉的那几条和原因，不是只说一句「删除完成」', async () => {
    bulkResponse = {
      requested: 3,
      deleted: 2,
      failures: [{ key: 't:命中1', reason: '该实体还被图谱里的关系边使用，无法删除' }],
    }
    const user = userEvent.setup()
    await searchForFlatList(user)
    await user.click(screen.getByRole('checkbox', { name: /选中本页/ }))
    await user.click(screen.getByRole('button', { name: /删除选中的 3 条/ }))
    await confirmDialog(user)

    const outcome = await screen.findByTestId('bulk-delete-outcome')
    // 成功数和请求数都要在——只报"删除了 2 条"的话，用户不知道自己请求的是 3 条。
    expect(outcome.textContent).toContain('成功 2 条')
    // 失败的那条被点名，且说清为什么。
    expect(outcome.textContent).toContain('t:命中1')
    expect(outcome.textContent).toContain('关系边')
  })

  it('全部成功时不报失败明细', async () => {
    const user = userEvent.setup()
    await searchForFlatList(user)
    await user.click(screen.getByRole('checkbox', { name: /选中本页/ }))
    await user.click(screen.getByRole('button', { name: /删除选中的 3 条/ }))
    await confirmDialog(user)

    const outcome = await screen.findByTestId('bulk-delete-outcome')
    expect(outcome.textContent).toContain('已删除 3 条')
    expect(outcome.textContent).not.toContain('没能删掉')
  })
})

describe('分组视图', () => {
  it('每个类型一条工具条，「全部」= 这个类型的全部而不是整租户', async () => {
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(screen.getByRole('group', { name: /订单号/ })).toBeTruthy())
    const orders = within(screen.getByRole('group', { name: /订单号/ }))
    await user.click(orders.getByRole('button', { name: /看样本|展开/ }))
    await waitFor(() => expect(orders.getByText('订单号-0')).toBeTruthy())

    await user.click(orders.getByRole('checkbox', { name: /选中本页/ }))
    await user.click(await orders.findByRole('button', { name: /改为选中筛选条件下的全部/ }))
    await user.click(orders.getByRole('button', { name: /删除选中的/ }))
    await confirmDialog(user)

    await waitFor(() => expect(bulkRequests.length).toBe(1))
    const body = bulkRequests[0].body as { filters?: { term_type?: string } }
    expect(body.filters?.term_type).toBe('订单号')
  })
})
