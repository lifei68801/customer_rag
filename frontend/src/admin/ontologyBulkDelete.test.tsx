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

/**
 * 本体结构页三张表的批量删除。
 *
 * 这三张表跟实体明细页不一样的两点，是这个文件主要钉住的东西：
 *
 * 1. **没有分页也没有筛选**，所以"选中的这些"和"全部"是同一件事，界面上
 *    只能有一档——出现第二个"改为选中全部"按钮就是给同一件事配了两个按钮；
 * 2. **守卫会挡住大多数删除**，所以失败明细才是主角：删 4 个类型可能 3 个
 *    被挡住，用户必须能逐条看到是谁挡的，退化成一句"删除失败"等于让他自己
 *    去猜。
 *
 * 每个批量用例的服务端返回都**同时**有删掉的和没删掉的：全成功或全失败的
 * 桩是假绿，那样"逐条渲染失败明细"和"一律报成功"两种实现都能过。
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

const TERM_TYPES = ['空闲甲', '空闲乙', 'module', '产品']

const RELATION_TYPES = ['SOLD_BY', 'PART_OF']

const CONSTRAINTS = [
  { subject_term_type: '产品', relation_type: 'SOLD_BY', object_term_type: '公司' },
  { subject_term_type: '公司', relation_type: 'SOLD_BY', object_term_type: '产品' },
]

/** 实体类型批量删除的返回：删掉 2 条，2 条被守卫挡住，原因逐条点名。 */
const TERM_TYPE_RESULT = {
  requested: 4,
  deleted: 2,
  failures: [
    {
      key: 'module',
      reason: "分类 'module' 仍被 1 条术语（示例登录模块）引用，无法删除",
    },
    {
      key: '产品',
      reason: "分类 '产品' 仍被 1 条关系约束（产品 -SOLD_BY-> 公司）引用，无法删除",
    },
  ],
}

const RELATION_TYPE_RESULT = {
  requested: 2,
  deleted: 1,
  failures: [{ key: 'PART_OF', reason: '关系类型不存在，可能已经被别人删掉了' }],
}

const CONSTRAINT_RESULT = {
  requested: 2,
  deleted: 1,
  failures: [
    { key: '公司 -SOLD_BY-> 产品', reason: '这条约束不存在，可能已经被别人删掉了' },
  ],
}

let bulkCalls: { url: string; body: unknown }[] = []

function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      const json = (body: unknown, status = 200) =>
        Promise.resolve(new Response(JSON.stringify(body), { status }))
      if (url.includes('/auth/whoami')) return whoamiResponse()
      if (url.includes('/nav-badges')) {
        return json({ pending_relations: 0, pending_duplicates: 0, total_terms: 1 })
      }
      if (/\/ontology\/[^/]+\/status$/.test(url)) return json({ confirmed: false })
      if (url.includes('/checkout')) return json({})
      if (url.includes('/terms/summary')) return json({ groups: [] })
      if (url.includes('bulk-delete') && method === 'POST') {
        bulkCalls.push({ url, body: JSON.parse(String(init?.body ?? '{}')) })
        if (url.includes('/term-types/')) return json(TERM_TYPE_RESULT)
        if (url.includes('/relation-types/')) return json(RELATION_TYPE_RESULT)
        return json(CONSTRAINT_RESULT)
      }
      if (url.includes('/term-types')) {
        return json({
          term_types: TERM_TYPES.map((value) => ({
            value,
            extra_fields: [],
            standard_name_value_type: 'string',
          })),
        })
      }
      if (url.includes('/relation-types')) {
        return json({
          relation_types: RELATION_TYPES.map((relation_type) => ({
            relation_type,
            example_phrase: '甲 关系 乙',
            description: '',
            allow_chain_query: true,
            source: 'manual',
          })),
        })
      }
      if (url.includes('/constraints')) return json({ constraints: CONSTRAINTS })
      if (url.includes('/terms')) return json({ terms: [], total: 0 })
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  bulkCalls = []
  resetAdminSession()
  sessionStorage.clear()
  localStorage.clear()
  stubApi()
})

function renderPage() {
  return render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[ADMIN_ROUTES.ontology]}>
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
}

async function confirmDialog(user: ReturnType<typeof userEvent.setup>) {
  const dialog = await screen.findByRole('alertdialog')
  const text = dialog.textContent ?? ''
  await user.click(within(dialog).getByRole('button', { name: '确认' }))
  return text
}

async function openTab(user: ReturnType<typeof userEvent.setup>, name: string) {
  const tabs = await screen.findByTestId('ontology-tabs')
  await user.click(within(tabs).getByRole('button', { name }))
}

describe('实体类型的批量删除', () => {
  it('表头复选框选中整张表，删掉的和被挡住的分开报，挡路的逐条点名', async () => {
    const user = userEvent.setup()
    renderPage()
    const selectAll = await screen.findByRole('checkbox', { name: /选中全部 4 条实体类型/ })
    await user.click(selectAll)
    await user.click(screen.getByRole('button', { name: /删除选中的 4 条/ }))
    const confirmText = await confirmDialog(user)
    expect(confirmText).toContain('4')
    expect(confirmText).toContain('实体类型')

    await waitFor(() => expect(bulkCalls).toHaveLength(1))
    expect(bulkCalls[0].url).toContain('/term-types/bulk-delete')
    expect(bulkCalls[0].body).toEqual({ values: TERM_TYPES })

    const outcome = await screen.findByTestId('bulk-delete-outcome')
    // 请求 4 条、成功 2 条都要说出来：只报"已删除 2 条"的话，另外 2 条就
    // 静默消失了。
    expect(outcome.textContent).toContain('4')
    expect(outcome.textContent).toContain('2')
    // 失败原因照抄后端那句点名的话，不是"删除失败"。
    expect(outcome.textContent).toContain('示例登录模块')
    expect(outcome.textContent).toContain('产品 -SOLD_BY-> 公司')
  })

  it('只勾一条时就只删这一条', async () => {
    const user = userEvent.setup()
    renderPage()
    const row = await screen.findByRole('checkbox', { name: '选中实体类型 module' })
    await user.click(row)
    await user.click(screen.getByRole('button', { name: /删除选中的 1 条/ }))
    await confirmDialog(user)

    await waitFor(() => expect(bulkCalls).toHaveLength(1))
    expect(bulkCalls[0].body).toEqual({ values: ['module'] })
  })

  it('没有第二档：不给"改为选中全部"这种按钮', async () => {
    // 这三张表一次性全量渲染，"本页"就是"全部"。同一件事配两个按钮，用户
    // 会先花时间去想它们的区别在哪，然后猜错。
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('checkbox', { name: /选中全部 4 条实体类型/ }))
    expect(screen.queryByRole('button', { name: /改为选中/ })).toBeNull()
  })

  it('确认框点取消就什么都不发', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('checkbox', { name: /选中全部 4 条实体类型/ }))
    await user.click(screen.getByRole('button', { name: /删除选中的 4 条/ }))
    const dialog = await screen.findByRole('alertdialog')
    await user.click(within(dialog).getByRole('button', { name: '取消' }))
    expect(bulkCalls).toHaveLength(0)
  })
})

describe('关系类型的批量删除', () => {
  it('发到关系类型自己的端点，失败的那条逐条报出来', async () => {
    const user = userEvent.setup()
    renderPage()
    await openTab(user, '关系类型')
    await user.click(await screen.findByRole('checkbox', { name: /选中全部 2 条关系类型/ }))
    await user.click(screen.getByRole('button', { name: /删除选中的 2 条/ }))
    await confirmDialog(user)

    await waitFor(() => expect(bulkCalls).toHaveLength(1))
    expect(bulkCalls[0].url).toContain('/relation-types/bulk-delete')
    expect(bulkCalls[0].body).toEqual({ relation_types: RELATION_TYPES })

    const outcome = await screen.findByTestId('bulk-delete-outcome')
    expect(outcome.textContent).toContain('PART_OF')
    expect(outcome.textContent).toContain('可能已经被别人删掉了')
  })
})

describe('关系约束的批量删除', () => {
  it('请求体里是三元组本身，失败明细按"主语 -关系-> 宾语"点名', async () => {
    const user = userEvent.setup()
    renderPage()
    await openTab(user, '约束')
    await user.click(await screen.findByRole('checkbox', { name: /选中全部 2 条约束/ }))
    await user.click(screen.getByRole('button', { name: /删除选中的 2 条/ }))
    await confirmDialog(user)

    await waitFor(() => expect(bulkCalls).toHaveLength(1))
    expect(bulkCalls[0].url).toContain('/constraints/bulk-delete')
    // 约束没有单列主键，服务端要的是三元组本身而不是那个显示用的字符串。
    expect(bulkCalls[0].body).toEqual({ constraints: CONSTRAINTS })

    const outcome = await screen.findByTestId('bulk-delete-outcome')
    expect(outcome.textContent).toContain('公司 -SOLD_BY-> 产品')
  })
})
