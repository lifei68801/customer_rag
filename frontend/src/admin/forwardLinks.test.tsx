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
 * 身份不再存 sessionStorage（token 在 HttpOnly Cookie 里，JS 读不到，也
 * 塞不进去）：界面从 whoami 拿身份，所以这里要打桩的是 whoami。
 */
let signedInRole: 'admin' | 'member' | null = null

function whoamiResponse() {
  if (signedInRole === null) {
    return Promise.resolve(new Response(JSON.stringify({ detail: '未登录' }), { status: 401 }))
  }
  return Promise.resolve(
    new Response(
      JSON.stringify({
        username: signedInRole === 'admin' ? 'admin' : 'alice',
        role: signedInRole,
        tenant_id: signedInRole === 'admin' ? null : 'demo',
        current_tenant_id: 'demo',
      }),
      { status: 200 },
    ),
  )
}

function signIn(role: 'admin' | 'member') {
  signedInRole = role
}

/**
 * 前向出口：第三条「这一步做完了，接下来去哪」。
 *
 * 后台已有两条出口——空状态告诉你该先去哪（emptyStateLinks.test.tsx 的
 * 约定），徽标告诉你有活等着（useNavBadges + NavBadge）。缺的是完成点：
 * 本体确认成功只弹一个 toast（几秒后自己消失），SchemaEtlPage 里
 * ADMIN_ROUTES 引用数为零，连它自己那句「请先完成本体 schema 确认后再
 * 触发 ETL」都不是链接。徽标补不上这个洞：待审关系能计数，「你该去传
 * 数据了」不能。
 */

interface StubRunSummary {
  run_id: string
  status: string
  started_at: string
  finished_at: string | null
}

let runsListResponse: StubRunSummary[] = []
let runDetailResponse: Record<string, unknown> | null = null

function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/auth/whoami')) return whoamiResponse()
      const method = init?.method ?? 'GET'
      const json = (body: unknown, status = 200) =>
        Promise.resolve(new Response(JSON.stringify(body), { status }))

      if (url.includes('/nav-badges')) {
        return json({ pending_relations: 0, pending_duplicates: 0, total_terms: 0 })
      }

      // ---- OntologySchemaPage 用到的接口 ----
      if (/\/ontology\/[^/]+\/status$/.test(url)) {
        return json({ confirmed: false })
      }
      if (url.includes('/ontology/') && url.includes('/checkout') && method === 'POST') {
        return json({})
      }
      if (url.includes('/ontology/') && url.includes('/confirm') && method === 'POST') {
        return json({})
      }
      // handleConfirm 在打开确认框之前会拉一次 /terms/summary（Task 6：确认
      // 框里附带数据影响），跟另外两路 snapshot 请求一起在同一个 Promise.all
      // 里——不 stub 它，未匹配分支的 new Promise(() => {}) 永不 resolve，
      // Promise.all 卡死，confirm() 从没被调用，alertdialog 永远不出现。
      if (url.includes('/terms/summary')) {
        return json({ groups: [] })
      }
      if (url.includes('/term-types')) {
        return json({
          term_types: [{ value: 'Product', extra_fields: [], standard_name_value_type: 'string' }],
        })
      }
      if (url.includes('/relation-types')) {
        return json({
          relation_types: [
            { relation_type: 'HAS', example_phrase: 'x has y', description: '', allow_chain_query: false },
          ],
        })
      }
      if (url.includes('/constraints')) {
        return json({
          constraints: [{ subject_term_type: 'Product', relation_type: 'HAS', object_term_type: 'Product' }],
        })
      }

      // ---- SchemaEtlPage 用到的接口。/etl-mapping 是上一个任务新加的——不
      // stub 它 mapping 会永远停在 undefined，页面卡在加载态，跟本任务无关。
      if (url.includes('/etl-mapping')) {
        return json({ mapping: null })
      }
      if (url.includes('/schema-etl/status')) {
        return json({ ontology_confirmed: true })
      }
      if (url.includes('/schema-etl/sample')) {
        return json({ files: [] })
      }
      if (/\/schema-etl\/runs\/[^/]+$/.test(url)) {
        return json(runDetailResponse ?? {})
      }
      if (url.includes('/schema-etl/runs')) {
        return json({ runs: runsListResponse })
      }

      // 现有 stub 范式：未匹配的 URL 永不 resolve，模拟"这条本来就不该被
      // 打到"或"未知态"。
      return new Promise(() => {})
    }),
  )
}

function stubCompletedRun(report: { dry_run: boolean }) {
  runsListResponse = [
    { run_id: 'run-1', status: 'completed', started_at: '2026-09-03T00:00:00', finished_at: '2026-09-03T00:05:00' },
  ]
  runDetailResponse = {
    run_id: 'run-1',
    status: 'completed',
    started_at: '2026-09-03T00:00:00',
    finished_at: '2026-09-03T00:05:00',
    error: null,
    report: {
      entities_written: 10,
      entities_skipped: 0,
      relations_written: 5,
      relations_skipped: 0,
      written_by_type: {},
      skipped_by_type: {},
      skipped_rows: [],
      skipped_mappings: [],
      entities_removed: 0,
      entities_removed_by_type: {},
      relations_removed: 0,
      dry_run: report.dry_run,
    },
  }
}

beforeEach(() => {
  signedInRole = null
  resetAdminSession()
  sessionStorage.clear()
  localStorage.clear()
  runsListResponse = []
  runDetailResponse = null
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

/** 点「确认 schema」，在弹出的确认框里点「确认」，走完整条确认流程。 */
async function confirmOntology() {
  const user = userEvent.setup()
  const button = await screen.findByRole('button', { name: /确认 schema/ })
  // 前置条件（三类草稿数据）异步查完之前按钮是禁用的——先等它变成可点。
  await waitFor(() => expect(button).not.toBeDisabled())
  await user.click(button)
  const dialog = await screen.findByRole('alertdialog')
  await user.click(within(dialog).getByRole('button', { name: '确认' }))
}

describe('前向出口', () => {
  it('本体确认成功后给出去表格导入的入口', async () => {
    signIn('admin')
    renderAt(ADMIN_ROUTES.ontology)
    await confirmOntology()
    // 「表格导入」在本页当前是唯一渲染的匹配（本体分组是当前组，展开的
    // 是「建模」而不是「接入数据」，侧边栏那份此时没挂载），但仍然收进
    // 常驻提示区查询，跟另外两条断言保持同一个纪律，不依赖侧边栏折叠
    // 状态这种隐含前提。
    const notice = await screen.findByTestId('just-confirmed-notice')
    const link = await within(notice).findByRole('link', { name: '表格导入' })
    expect(link.getAttribute('href')).toBe(ADMIN_ROUTES.etl)
  })

  it('表格导入成功跑完后给出去看结果的入口', async () => {
    signIn('admin')
    const user = userEvent.setup()
    stubCompletedRun({ dry_run: false })
    renderAt(ADMIN_ROUTES.etl)
    // 选中这条跑批记录，才会渲染详情区——出口挂在详情区里。
    await user.click(await screen.findByText('run-1'))
    // 「实体列表」同时也是侧边栏 NAV_STANDALONE 里始终常驻的一个链接——
    // 不管本任务改没改对，那个链接都在，直接用 screen 查会假绿。这里
    // 把查询范围收进跑批详情区（data-testid="etl-run-detail"），只有
    // 那里面的链接才是本任务加的出口。
    const detail = await screen.findByTestId('etl-run-detail')
    const link = await within(detail).findByRole('link', { name: '实体列表' })
    expect(link.getAttribute('href')).toBe(ADMIN_ROUTES.terms)
    const reviewLink = await within(detail).findByRole('link', { name: '待审关系' })
    expect(reviewLink.getAttribute('href')).toBe(ADMIN_ROUTES.reviewRelations)
  })

  it('预演跑完不给"去看结果"——什么都还没写进去', async () => {
    signIn('admin')
    const user = userEvent.setup()
    stubCompletedRun({ dry_run: true })
    renderAt(ADMIN_ROUTES.etl)
    await user.click(await screen.findByText('run-1'))
    const detail = await screen.findByTestId('etl-run-detail')
    await within(detail).findByText(/这是一次预演/)
    // 同上：查询范围收进详情区，避免撞上侧边栏常驻的「实体列表」链接。
    expect(within(detail).queryByRole('link', { name: '实体列表' })).toBeNull()
  })
})
