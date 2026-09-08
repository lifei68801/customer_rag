import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { resetAdminSession } from './useAdminAuth'

/**
 * 实体详情页的批量删关系边。
 *
 * 一个实体的关系边是全量渲染的，既没有分页也没有筛选：「本页」和「全部」
 * 是同一批东西，所以这里只做**一档**——同一件事摆两个按钮，用户会先花时间
 * 去想区别在哪，然后猜错。
 *
 * 出边和入边虽然分两组展示，但属于同一档：它们是同一个实体身上的同一批边，
 * 分成两档选中会让"删掉选中的"到底指哪些变得含糊。
 *
 * 租户标记异常的那批边是独立的一档：它们走另一个端点，授权判据也不同。
 */

const NODE_KEY = '公司:可口可乐'
const PATH = `/admin/terms/${encodeURIComponent(NODE_KEY)}`

const term = {
  node_key: NODE_KEY,
  standard_name: '可口可乐',
  aliases: [],
  term_type: '公司',
  extra_properties: {},
  source: 'etl',
  relations: [
    { direction: 'out', relation_type: '生产', node_key: '产品:雪碧', standard_name: '雪碧', term_type: '产品' },
    { direction: 'out', relation_type: '生产', node_key: '产品:芬达', standard_name: '芬达', term_type: '产品' },
    { direction: 'in', relation_type: '隶属', node_key: '类目:饮料', standard_name: '饮料', term_type: '类目' },
  ],
}

const inconsistentRelations = [
  {
    direction: 'out',
    relation_type: 'RELATED_TO',
    node_key: '公司:装瓶厂',
    standard_name: '装瓶厂',
    term_type: '公司',
    other_tenant_id: 'demo',
    edge_tenant_id: 'legacy',
    category: 'edge_tenant_mismatch',
    deletable: true,
  },
  {
    direction: 'in',
    relation_type: 'PART_OF',
    node_key: '公司:别家的',
    standard_name: '别家的',
    term_type: '公司',
    other_tenant_id: 'tenant_b',
    edge_tenant_id: 'tenant_b',
    category: 'cross_tenant',
    deletable: true,
  },
]

let bulkRequests: { url: string; body: Record<string, unknown> }[] = []
let relationBulkResponse = {
  requested: 3,
  deleted: 3,
  failures: [] as { key: string; reason: string }[],
}
let inconsistentBulkResponse = {
  requested: 2,
  deleted: 2,
  failures: [] as { key: string; reason: string }[],
}

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

function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = decodeURIComponent(String(input))
      if (url.includes('/auth/whoami')) return whoamiResponse()
      // 顺序要紧：这几条 URL 互为前缀，长的必须先判。
      if (url.includes('/relations/inconsistent/bulk-delete')) {
        bulkRequests.push({ url, body: JSON.parse(String(init?.body ?? '{}')) })
        return Promise.resolve(
          new Response(JSON.stringify(inconsistentBulkResponse), { status: 200 }),
        )
      }
      if (url.includes('/relations/bulk-delete')) {
        bulkRequests.push({ url, body: JSON.parse(String(init?.body ?? '{}')) })
        return Promise.resolve(new Response(JSON.stringify(relationBulkResponse), { status: 200 }))
      }
      if (url.includes('/relations/inconsistent')) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ inconsistent_relations: inconsistentRelations }),
            { status: 200 },
          ),
        )
      }
      if (url.includes('/terms/')) {
        return Promise.resolve(new Response(JSON.stringify(term), { status: 200 }))
      }
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  bulkRequests = []
  relationBulkResponse = { requested: 3, deleted: 3, failures: [] }
  inconsistentBulkResponse = { requested: 2, deleted: 2, failures: [] }
  resetAdminSession()
  localStorage.clear()
  stubApi()
})

function renderPage() {
  return render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[PATH]}>
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
}

async function openDetail() {
  renderPage()
  await waitFor(() => expect(screen.getByTestId('term-detail')).toBeTruthy())
  await waitFor(() => expect(screen.getByTestId('relations-bulk')).toBeTruthy())
}

async function confirmDialog(user: ReturnType<typeof userEvent.setup>) {
  const dialog = await screen.findByRole('alertdialog')
  const text = dialog.textContent ?? ''
  await user.click(within(dialog).getByRole('button', { name: '确认' }))
  return text
}

const relationsBar = () => within(screen.getByTestId('relations-bulk'))
const inconsistentBar = () => within(screen.getByTestId('inconsistent-relations-bulk'))

describe('图谱关系：只有一档', () => {
  it('不给「改为选中全部」的入口——列出来的就是全部', async () => {
    const user = userEvent.setup()
    await openDetail()

    await user.click(relationsBar().getByRole('checkbox', { name: /选中/ }))

    expect(relationsBar().queryByRole('button', { name: /改为选中筛选条件下的全部/ })).toBeNull()
  })

  it('出边和入边归同一档：表头一勾，两组的边一起被选上', async () => {
    // 分成两档的话，"删掉选中的"到底指哪些会变含糊——而删边不可逆。
    const user = userEvent.setup()
    await openDetail()

    await user.click(relationsBar().getByRole('checkbox', { name: /选中/ }))
    await user.click(relationsBar().getByRole('button', { name: /删除选中的 3 条/ }))
    await confirmDialog(user)

    await waitFor(() => expect(bulkRequests.length).toBe(1))
    expect(bulkRequests[0].url).toContain(`/terms/${NODE_KEY}/relations/bulk-delete`)
    expect((bulkRequests[0].body as { edges?: unknown[] }).edges).toHaveLength(3)
  })

  it('每条边照它在界面上的方向发出去，不是统统当成出边', async () => {
    // direction 写反了指的就是另一条边——对同一对实体之间的双向关系来说，
    // 那正是用户没勾的那一条。
    const user = userEvent.setup()
    await openDetail()

    await user.click(relationsBar().getByRole('checkbox', { name: /选中/ }))
    await user.click(relationsBar().getByRole('button', { name: /删除选中的 3 条/ }))
    await confirmDialog(user)

    await waitFor(() => expect(bulkRequests.length).toBe(1))
    const edges = (bulkRequests[0].body as {
      edges: { direction: string; relation_type: string; other_node_key: string }[]
    }).edges
    expect(edges).toEqual(
      expect.arrayContaining([
        { direction: 'out', relation_type: '生产', other_node_key: '产品:雪碧' },
        { direction: 'out', relation_type: '生产', other_node_key: '产品:芬达' },
        { direction: 'in', relation_type: '隶属', other_node_key: '类目:饮料' },
      ]),
    )
  })

  it('逐条勾选时只发勾上的那几条', async () => {
    const user = userEvent.setup()
    await openDetail()

    await user.click(screen.getByRole('checkbox', { name: '选中关系 可口可乐 -生产-> 雪碧' }))
    await user.click(relationsBar().getByRole('button', { name: /删除选中的 1 条/ }))
    await confirmDialog(user)

    await waitFor(() => expect(bulkRequests.length).toBe(1))
    expect((bulkRequests[0].body as { edges: unknown[] }).edges).toEqual([
      { direction: 'out', relation_type: '生产', other_node_key: '产品:雪碧' },
    ])
  })

  it('确认框写清条数和不可撤销', async () => {
    const user = userEvent.setup()
    await openDetail()
    await user.click(relationsBar().getByRole('checkbox', { name: /选中/ }))
    await user.click(relationsBar().getByRole('button', { name: /删除选中的 3 条/ }))

    const text = await confirmDialog(user)
    expect(text).toContain('3 条关系边')
    expect(text).toContain('不可撤销')
  })

  it('部分失败时逐条列出没删掉的那条边，写成「主语 -类型-> 宾语」', async () => {
    // 关系边不是一个简单 id，只说"有一条没删掉"用户没法知道是哪条。
    relationBulkResponse = {
      requested: 3,
      deleted: 2,
      failures: [
        { key: '公司:可口可乐 -生产-> 产品:芬达', reason: '没有找到这条关系，它可能已经被删掉了' },
      ],
    }
    const user = userEvent.setup()
    await openDetail()
    await user.click(relationsBar().getByRole('checkbox', { name: /选中/ }))
    await user.click(relationsBar().getByRole('button', { name: /删除选中的 3 条/ }))
    await confirmDialog(user)

    const outcome = await screen.findByTestId('bulk-delete-outcome')
    expect(outcome.textContent).toContain('成功 2 条')
    expect(outcome.textContent).toContain('公司:可口可乐 -生产-> 产品:芬达')
    expect(outcome.textContent).toContain('没有找到')
  })
})

describe('租户标记异常的关系边：单独一档、单独的端点', () => {
  it('走的是 inconsistent 那条端点，而且带上对端的租户', async () => {
    // 这类边定位靠的是两端节点各自的租户——边自己标的那个值恰恰就是
    // 错的那个属性。少带 other_tenant_id 就一条也匹配不上。
    const user = userEvent.setup()
    await openDetail()

    await user.click(inconsistentBar().getByRole('checkbox', { name: /选中/ }))
    await user.click(inconsistentBar().getByRole('button', { name: /删除选中的 2 条/ }))
    await confirmDialog(user)

    await waitFor(() => expect(bulkRequests.length).toBe(1))
    expect(bulkRequests[0].url).toContain('/relations/inconsistent/bulk-delete')
    expect((bulkRequests[0].body as { edges: unknown[] }).edges).toEqual(
      expect.arrayContaining([
        {
          direction: 'out',
          relation_type: 'RELATED_TO',
          other_node_key: '公司:装瓶厂',
          other_tenant_id: 'demo',
        },
        {
          direction: 'in',
          relation_type: 'PART_OF',
          other_node_key: '公司:别家的',
          other_tenant_id: 'tenant_b',
        },
      ]),
    )
  })

  it('两档互不牵连：选了异常边那一档，正常关系那一档不显示选中', async () => {
    const user = userEvent.setup()
    await openDetail()

    await user.click(inconsistentBar().getByRole('checkbox', { name: /选中/ }))

    expect(screen.queryByTestId('bulk-selection-notice-relations')).toBeNull()
    expect(screen.getByTestId('bulk-selection-notice-inconsistent-relations')).toBeTruthy()
  })

  it('部分失败时点名是哪条边，跨租户那条的理由要说到管理员', async () => {
    inconsistentBulkResponse = {
      requested: 2,
      deleted: 1,
      failures: [
        {
          key: '公司:别家的 -PART_OF-> 公司:可口可乐',
          reason:
            '这条关系边的另一端属于其他租户，删掉它会同时改变那个租户的图谱——只有平台管理员能处理这一类边',
        },
      ],
    }
    const user = userEvent.setup()
    await openDetail()
    await user.click(inconsistentBar().getByRole('checkbox', { name: /选中/ }))
    await user.click(inconsistentBar().getByRole('button', { name: /删除选中的 2 条/ }))
    await confirmDialog(user)

    const outcome = await screen.findByTestId('bulk-delete-outcome')
    expect(outcome.textContent).toContain('成功 1 条')
    expect(outcome.textContent).toContain('公司:别家的 -PART_OF-> 公司:可口可乐')
    expect(outcome.textContent).toContain('平台管理员')
  })
})
