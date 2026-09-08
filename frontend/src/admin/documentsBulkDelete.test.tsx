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
 * 文档页的批量删除，两个删除点各一档结构：
 *
 * * **已摄取文档**有分页、没有筛选，所以是两档——「本页 3 条」和「整租户
 *   的全部 7 条」。两个数字故意不同：相等的话「删本页」和「删全部」两种
 *   实现都能让断言变绿。
 * * **摄取任务**既不分页也不筛选，「本页」和「全部」是同一批东西，只做
 *   一档——界面上不该出现两个按钮干同一件事。
 *
 * 任务删除有连带副作用（关联的上传文件会被清理），确认框必须说出来：
 * 中途有一条删不掉时，已经清掉的文件不会"回滚"。
 */

const PAGE_ROWS = 3
const TOTAL_DOCUMENTS = 7

const DOC_DIR = 'uploads/t1'
const docNames = ['9f_报表.md', 'a1_合同.pdf', 'b2_纪要.docx']
const docs = docNames.map((name, i) => ({
  file_path: `${DOC_DIR}/${name}`,
  content_hash: `h${i}`,
  chunk_count: 3,
  last_ingested_at: '2026-09-01 10:00:00',
  graph_status: 'built',
}))

// 处理中的任务里只有 is_stuck 的那条才有删除入口——正在正常处理的任务
// 删掉会留下写了一半的向量数据。
const pendingJobs = [
  { job_id: 'job-stuck', file_path: `${DOC_DIR}/c3_卡住的.md`, status: 'pending', last_error: null, is_stuck: true },
  { job_id: 'job-running', file_path: `${DOC_DIR}/d4_正在跑.md`, status: 'pending', last_error: null, is_stuck: false },
]
const deadJobs = [
  { job_id: 'job-dead-1', file_path: `${DOC_DIR}/e5_失败一.md`, last_error: '解析失败' },
  { job_id: 'job-dead-2', file_path: `${DOC_DIR}/f6_失败二.md`, last_error: '解析失败' },
]

let bulkRequests: { url: string; body: Record<string, unknown> }[] = []
let docBulkResponse = { requested: 3, deleted: 3, failures: [] as { key: string; reason: string }[] }
let jobBulkResponse = { requested: 2, deleted: 2, failures: [] as { key: string; reason: string }[] }

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
      const url = String(input)
      if (url.includes('/auth/whoami')) return whoamiResponse()
      if (url.includes('/api/admin/tenants')) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ tenants: [{ tenant_id: 'demo', name: 'demo', status: 'active' }] }),
            { status: 200 },
          ),
        )
      }
      if (url.includes('/ontology/') && url.includes('/status')) {
        return Promise.resolve(new Response(JSON.stringify({ confirmed: true }), { status: 200 }))
      }
      // 两条批量端点必须排在 /documents 前面：它们的 URL 也包含 /documents。
      if (url.includes('/documents/jobs/bulk-delete')) {
        bulkRequests.push({ url, body: JSON.parse(String(init?.body ?? '{}')) })
        return Promise.resolve(new Response(JSON.stringify(jobBulkResponse), { status: 200 }))
      }
      if (url.includes('/documents/bulk-delete')) {
        bulkRequests.push({ url, body: JSON.parse(String(init?.body ?? '{}')) })
        return Promise.resolve(new Response(JSON.stringify(docBulkResponse), { status: 200 }))
      }
      if (url.includes('/documents')) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              documents: docs,
              total: TOTAL_DOCUMENTS,
              pending_jobs: pendingJobs,
              dead_jobs: deadJobs,
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
  docBulkResponse = { requested: 3, deleted: 3, failures: [] }
  jobBulkResponse = { requested: 2, deleted: 2, failures: [] }
  resetAdminSession()
  localStorage.clear()
  stubApi()
})

function renderPage() {
  return render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[ADMIN_ROUTES.documents]}>
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
}

async function openPage() {
  renderPage()
  await waitFor(() => expect(screen.getByText(/9f_报表\.md/)).toBeTruthy())
}

async function confirmDialog(user: ReturnType<typeof userEvent.setup>) {
  const dialog = await screen.findByRole('alertdialog')
  const text = dialog.textContent ?? ''
  await user.click(within(dialog).getByRole('button', { name: '确认' }))
  return text
}

const documentsSection = () => within(screen.getByTestId('documents-bulk'))
const stuckSection = () => within(screen.getByTestId('stuck-jobs-bulk'))
const deadSection = () => within(screen.getByTestId('dead-jobs-bulk'))

describe('已摄取文档：本页和整租户全部是两档', () => {
  it('表头复选框勾的是本页，提示条说出本页条数', async () => {
    const user = userEvent.setup()
    await openPage()

    await user.click(documentsSection().getByRole('checkbox', { name: /选中本页/ }))

    const notice = await screen.findByTestId('bulk-selection-notice-documents')
    expect(notice.textContent).toContain(`已选中本页 ${PAGE_ROWS} 条`)
  })

  it('给出升级入口，写的是整租户的真实条数而不是本页那 3 条', async () => {
    const user = userEvent.setup()
    await openPage()

    await user.click(documentsSection().getByRole('checkbox', { name: /选中本页/ }))

    expect(
      await documentsSection().findByRole('button', {
        name: new RegExp(`改为选中筛选条件下的全部 ${TOTAL_DOCUMENTS} 条`),
      }),
    ).toBeTruthy()
  })

  it('本页全选发的是 file_paths，不是筛选条件', async () => {
    const user = userEvent.setup()
    await openPage()
    await user.click(documentsSection().getByRole('checkbox', { name: /选中本页/ }))
    await user.click(documentsSection().getByRole('button', { name: /删除选中的 3 条/ }))
    await confirmDialog(user)

    await waitFor(() => expect(bulkRequests.length).toBe(1))
    const body = bulkRequests[0].body as { file_paths?: string[]; filters?: unknown }
    expect(body.file_paths).toEqual(docs.map((d) => d.file_path))
    // 两种模式互斥，后端同时收到会报 400。
    expect(body.filters).toBeUndefined()
  })

  it('「全部」发的是筛选条件，不是先把 7 条拉回来再逐条删', async () => {
    const user = userEvent.setup()
    await openPage()
    await user.click(documentsSection().getByRole('checkbox', { name: /选中本页/ }))
    await user.click(
      await documentsSection().findByRole('button', { name: /改为选中筛选条件下的全部/ }),
    )
    await user.click(documentsSection().getByRole('button', { name: /删除选中的/ }))
    const text = await confirmDialog(user)

    await waitFor(() => expect(bulkRequests.length).toBe(1))
    const body = bulkRequests[0].body as { file_paths?: string[]; filters?: unknown }
    expect(body.file_paths).toBeUndefined()
    expect(body.filters).toEqual({})
    // 确认框要说清这是整租户的全部多少条——只问一句"确定删除全部吗"的话，
    // 用户分不出自己按下的是 3 条还是 7 条。
    expect(text).toContain(`这个租户的全部 ${TOTAL_DOCUMENTS} 条文档`)
  })

  it('失败明细按界面上看到的文件名点名，不是甩一条完整落盘路径', async () => {
    docBulkResponse = {
      requested: 3,
      deleted: 2,
      failures: [{ key: `${DOC_DIR}/${docNames[1]}`, reason: '删除向量数据失败：milvus 连接失败' }],
    }
    const user = userEvent.setup()
    await openPage()
    await user.click(documentsSection().getByRole('checkbox', { name: /选中本页/ }))
    await user.click(documentsSection().getByRole('button', { name: /删除选中的 3 条/ }))
    await confirmDialog(user)

    const outcome = await screen.findByTestId('bulk-delete-outcome')
    // 请求数和成功数都要在：只报"删除了 2 条"用户不知道自己请求的是 3 条。
    expect(outcome.textContent).toContain('成功 2 条')
    expect(outcome.textContent).toContain(docNames[1])
    expect(outcome.textContent).toContain('milvus')
    expect(outcome.textContent).not.toContain(DOC_DIR)
  })
})

describe('摄取任务：只有一档', () => {
  it('不给「改为选中全部」的入口——本页就是全部，两个按钮干同一件事', async () => {
    const user = userEvent.setup()
    await openPage()

    await user.click(deadSection().getByRole('checkbox', { name: /选中本页/ }))

    expect(deadSection().queryByRole('button', { name: /改为选中筛选条件下的全部/ })).toBeNull()
  })

  it('只有疑似卡死的那条处理中任务可以被选中', async () => {
    // 正在正常处理的任务删掉会留下写了一半的向量数据——它连删除按钮都
    // 没有，批量选择也不该把它捎上。
    const user = userEvent.setup()
    await openPage()

    await user.click(stuckSection().getByRole('checkbox', { name: /选中本页/ }))
    await user.click(stuckSection().getByRole('button', { name: /删除选中的/ }))
    await confirmDialog(user)

    await waitFor(() => expect(bulkRequests.length).toBe(1))
    expect(bulkRequests[0].url).toContain('/documents/jobs/bulk-delete')
    expect((bulkRequests[0].body as { job_ids?: string[] }).job_ids).toEqual(['job-stuck'])
  })

  it('确认框说出连带清理的上传文件，以及它不会回滚', async () => {
    // 这一步会逐条删掉磁盘上的原始文件。中途有一条删不掉时，前面已经
    // 清掉的文件找不回来——用户在按下确认之前必须知道这件事。
    const user = userEvent.setup()
    await openPage()
    await user.click(deadSection().getByRole('checkbox', { name: /选中本页/ }))
    await user.click(deadSection().getByRole('button', { name: /删除选中的 2 条/ }))

    const text = await confirmDialog(user)
    expect(text).toContain('上传文件')
    expect(text).toContain('回滚')
  })

  it('部分失败时逐条列出没删掉的那条，按文件名而不是任务 id', async () => {
    jobBulkResponse = {
      requested: 2,
      deleted: 1,
      failures: [
        {
          key: 'job-dead-2',
          reason: '该任务当前不是失败状态、也不是疑似卡死的处理中状态，无法删除',
        },
      ],
    }
    const user = userEvent.setup()
    await openPage()
    await user.click(deadSection().getByRole('checkbox', { name: /选中本页/ }))
    await user.click(deadSection().getByRole('button', { name: /删除选中的 2 条/ }))
    await confirmDialog(user)

    const outcome = await screen.findByTestId('bulk-delete-outcome')
    expect(outcome.textContent).toContain('成功 1 条')
    expect(outcome.textContent).toContain('f6_失败二.md')
    expect(outcome.textContent).toContain('无法删除')
    // 裸 uuid 对用户没有意义，界面上那一行写的也不是它。
    expect(outcome.textContent).not.toContain('job-dead-2')
  })
})
