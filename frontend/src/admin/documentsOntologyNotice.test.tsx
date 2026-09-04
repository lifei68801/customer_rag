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

/**
 * 本体未确认时，「同时构建知识图谱」是一句空承诺。
 *
 * `ingestion/pipeline.py:108`：本体未确认时跳过图谱抽取，只写一条
 * log.info。用户勾上这个复选框、接受了「耗时更久」，然后什么图谱都没建，
 * 界面还显示「已摄取」。他不会怀疑到本体上。
 *
 * 关掉的只是这个选项，不是整个上传：分块和向量化不依赖本体，文档照样
 * 能被检索到。ETL 那边禁用整个操作是对的（没有 schema 就没法把列映射到
 * 实体类型），文档上传只有图谱那一半依赖本体。
 */

// byTenant 让确认状态随租户变化——「切到未确认的租户」是唯一能产生
// 「已勾选 + 本体未确认」这个组合的路径。
interface StubDoc {
  file_path: string
  content_hash: string
  chunk_count: number
  last_ingested_at: string
  graph_status: string | null
}

function stubApi(
  confirmed: boolean | 'error',
  byTenant?: Record<string, boolean>,
  docs: StubDoc[] = [],
) {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/auth/whoami')) return whoamiResponse()
      // 切租户走服务端：成功了界面才跟着变。
      if (url.includes('/auth/session/tenant')) {
        return Promise.resolve(new Response(JSON.stringify({}), { status: 200 }))
      }
      if (url.includes('/api/admin/tenants')) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              tenants: [
                { tenant_id: 'demo', name: 'demo', status: 'active' },
                { tenant_id: 'fresh', name: 'fresh', status: 'active' },
              ],
            }),
            { status: 200 },
          ),
        )
      }
      if (url.includes('/ontology/') && url.includes('/status')) {
        if (confirmed === 'error') return Promise.reject(new Error('boom'))
        const tenant = url.match(/\/ontology\/([^/]+)\/status/)?.[1] ?? ''
        const value = byTenant ? (byTenant[tenant] ?? confirmed) : confirmed
        return Promise.resolve(new Response(JSON.stringify({ confirmed: value }), { status: 200 }))
      }
      if (url.includes('/documents')) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ documents: docs, total: docs.length, pending_jobs: [], dead_jobs: [] }),
            { status: 200 },
          ),
        )
      }
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  resetAdminSession()
  // 这些用例要测的是切租户/导航的行为，需要管理员身份——member 的租户
  // 是登录时绑定的，切换这个能力对它不存在。
  localStorage.clear()
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

const graphBox = () => screen.getByRole('checkbox', { name: /知识图谱/ })
const upload = () => screen.getByRole('button', { name: /上传文档/ })
const notice = () => screen.queryByTestId('graph-unavailable')

async function ready() {
  await waitFor(() => expect(screen.getByRole('checkbox', { name: /知识图谱/ })).toBeTruthy())
}

describe('本体未确认', () => {
  it('关掉图谱选项，不关掉上传', async () => {
    stubApi(false)
    renderPage()
    await ready()
    await waitFor(() => expect(graphBox()).toBeDisabled())
    expect(upload()).toBeEnabled()
  })

  it('说明为什么，以及仍然会发生什么', async () => {
    stubApi(false)
    renderPage()
    await ready()
    await waitFor(() => expect(notice()).toBeTruthy())
    const text = notice()!.textContent ?? ''
    // 只说"不会建图谱"会让用户以为上传白做了。两件事都得说。
    expect(text, '没说原因').toMatch(/本体/)
    expect(text, '没说向量检索仍然有效').toMatch(/检索/)
  })

  it('给出去确认本体的入口', async () => {
    // 告诉用户出了什么事而不告诉他去哪修，等于让他自己找。
    stubApi(false)
    renderPage()
    await ready()
    await waitFor(() => expect(notice()).toBeTruthy())
    expect(screen.getByRole('link', { name: /本体结构/ })).toBeTruthy()
  })

  it('切到未确认的租户时，已勾上的要被撤掉', async () => {
    // disabled 的受控 checkbox 仍然会把 build_graph=true 提交上去（值来自
    // React state，不是 DOM），所以只变灰等于视觉上关了、实际没关。
    //
    // 切租户是唯一能产生这个组合的路径：在已确认的租户里勾上，切到一个
    // 没确认的租户。重新挂载测不出来——那时 buildGraph 本来就是初始的
    // false，测试会假绿。
    const user = userEvent.setup()
    stubApi(true, { demo: true, fresh: false })
    renderPage()
    await ready()

    await user.click(graphBox())
    expect(graphBox()).toBeChecked()

    // 租户切换现在在左下角的账号菜单里，不再是一个 select。
    await user.click(screen.getByRole('button', { name: /账号与租户/ }))
    await user.click(
      within(screen.getByRole('menu', { name: '账号与租户' })).getByRole('menuitemradio', {
        name: /fresh/,
      }),
    )

    await waitFor(() => expect(graphBox()).toBeDisabled())
    expect(graphBox(), '切到未确认租户后仍然勾着').not.toBeChecked()
  })
})

describe('本体已确认', () => {
  it('复选框可用，不显示提示', async () => {
    stubApi(true)
    renderPage()
    await ready()
    expect(graphBox()).toBeEnabled()
    expect(notice()).toBeNull()
  })
})

describe('状态拉不到', () => {
  it('保持可用，不显示提示', async () => {
    // 拉不到就断言"图谱不会被抽取"是在编一个可能不实的说法。宁可什么都
    // 不说，也不说错——跟侧边栏徽标同一条规矩。
    stubApi('error')
    renderPage()
    await ready()
    expect(graphBox()).toBeEnabled()
    expect(notice()).toBeNull()
  })
})

describe('已摄取列表标出没有图谱的文档', () => {
  // 上传前的提示只覆盖「接下来会怎样」。已经传完的那批，用户事后无从知道
  // 哪些没有图谱——只能整批重传。这一列就是给事后查的。
  const doc = (file: string, graph_status: string | null): StubDoc => ({
    file_path: `/data/${file}`,
    content_hash: 'h',
    chunk_count: 3,
    last_ingested_at: '2026-09-01T10:00:00',
    graph_status,
  })

  it('被跳过的那条要说出来', async () => {
    stubApi(true, undefined, [doc('a.md', 'skipped_ontology_unconfirmed')])
    renderPage()
    await waitFor(() => expect(screen.getByText(/a\.md/)).toBeTruthy())
    expect(screen.getByTitle(/本体.*未确认/)).toBeTruthy()
  })

  it('建好了的不加标记——正常状态不需要解释', async () => {
    stubApi(true, undefined, [doc('a.md', 'built')])
    renderPage()
    await waitFor(() => expect(screen.getByText(/a\.md/)).toBeTruthy())
    expect(screen.queryByTitle(/本体.*未确认/)).toBeNull()
  })

  it('用户没要求建图的也不加标记', async () => {
    // not_requested 是用户自己的选择，报警是噪音。
    stubApi(true, undefined, [doc('a.md', 'not_requested')])
    renderPage()
    await waitFor(() => expect(screen.getByText(/a\.md/)).toBeTruthy())
    expect(screen.queryByTitle(/本体.*未确认/)).toBeNull()
  })

  it('历史记录（状态未知）也不加标记', async () => {
    // NULL 是「不知道建没建」，不是「确定没建」。拿不准就别说——
    // 跟徽标、跟上传前的提示同一条规矩。
    stubApi(true, undefined, [doc('old.md', null)])
    renderPage()
    await waitFor(() => expect(screen.getByText(/old\.md/)).toBeTruthy())
    expect(screen.queryByTitle(/本体.*未确认/)).toBeNull()
  })
})
