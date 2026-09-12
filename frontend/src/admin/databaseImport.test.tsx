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
import { PasswordPrompt } from './dbImport/PasswordPrompt'

/**
 * 数据库导入页。
 *
 * **核心承诺是「库里零密码」**（spec D3）：保存数据源的请求体里不该有 password，
 * 重新同步时现填、用完即丢，且绝不进 URL——URL 会进浏览器历史、进服务端访问
 * 日志、进 Referer 头。
 */

interface Recorded {
  url: string
  method: string
  body: string
}

let requests: Recorded[] = []
let sources: unknown[]
//: 已确认的实体类型。null = 这个接口不应答（默认），于是页面退回自由文本
//: ——既有用例都是在这个状态下跑的，保持它们原样。
let confirmedTermTypes: string[] | null = null
let testConnectionOk: boolean
let connectionError: string

function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      requests.push({ url, method, body: String(init?.body ?? '') })
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
      if (url.includes('/db-import/test-connection')) {
        return Promise.resolve(
          testConnectionOk
            ? new Response(JSON.stringify({ ok: true }), { status: 200 })
            : new Response(JSON.stringify({ detail: connectionError }), { status: 400 }),
        )
      }
      if (url.includes('/db-import/preview')) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              columns: ['id', 'name'],
              rows: [
                [1, '可乐'],
                [2, '雪碧'],
              ],
              row_count: 2,
              truncated: false,
            }),
            { status: 200 },
          ),
        )
      }
      if (url.includes('/db-import/sources') && method === 'POST' && url.endsWith('/sync')) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ row_count: 2, entities_written: 2, entities_skipped: 0 }),
            { status: 200 },
          ),
        )
      }
      if (url.includes('/db-import/sources') && method === 'POST') {
        return Promise.resolve(new Response(JSON.stringify({ source_id: 's1' }), { status: 200 }))
      }
      if (url.includes('/db-import/sources')) {
        return Promise.resolve(new Response(JSON.stringify({ items: sources }), { status: 200 }))
      }
      if (url.includes('/nav-badges')) {
        return Promise.resolve(new Response(JSON.stringify({}), { status: 200 }))
      }
      if (url.includes('/term-types') && confirmedTermTypes !== null) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ term_types: confirmedTermTypes.map((value) => ({ value })) }),
            { status: 200 },
          ),
        )
      }
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  requests = []
  confirmedTermTypes = null
  testConnectionOk = true
  connectionError = '连不上 10.0.0.5:3306（OperationalError）。请检查地址、端口、账号密码。'
  sources = [
    {
      source_id: 's1',
      name: '商品库',
      driver: 'mysql',
      host: '10.0.0.5',
      port: 3306,
      database: 'shop',
      username: 'reader',
      query: 'SELECT id, name FROM goods',
      mapping: { term_type: '产品' },
      last_sync_at: '2026-09-08 10:00:00',
      last_sync_rows: 4712,
    },
  ]
  resetAdminSession()
  localStorage.clear()
  stubApi()
})

async function renderPage() {
  const result = render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[ADMIN_ROUTES.dbImport]}>
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
  await screen.findByTestId('admin-topbar')
  return result
}

/** 走完向导的前两步：填连接 → 测连通。 */
async function fillConnectionAndTest(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('button', { name: /新建数据源/ }))
  await user.type(screen.getByLabelText('主机'), '10.0.0.5')
  await user.type(screen.getByLabelText('端口'), '3306')
  await user.type(screen.getByLabelText('库名'), 'shop')
  await user.type(screen.getByLabelText('账号'), 'reader')
  await user.type(screen.getByLabelText('密码'), 'hunter2')
  await user.click(screen.getByRole('button', { name: '测连通' }))
}

describe('数据库导入页', () => {
  it('测连通之前不让进下一步', async () => {
    // 没测就往下走的话，用户会在写完 SQL、配完映射之后才发现连不上——
    // 前面两步全白做。
    const user = userEvent.setup()
    await renderPage()

    await user.click(await screen.findByRole('button', { name: /新建数据源/ }))

    expect((screen.getByRole('button', { name: '下一步' }) as HTMLButtonElement).disabled).toBe(
      true,
    )

    await user.type(screen.getByLabelText('主机'), '10.0.0.5')
    await user.type(screen.getByLabelText('密码'), 'hunter2')
    await user.click(screen.getByRole('button', { name: '测连通' }))

    await waitFor(() =>
      expect((screen.getByRole('button', { name: '下一步' }) as HTMLButtonElement).disabled).toBe(
        false,
      ),
    )
  })

  it('连不通时把后端那句话原样显示', async () => {
    // 「连不上 10.0.0.5:3306」比「连接失败」有用得多——用户据此判断是
    // 地址写错了还是网络不通。
    testConnectionOk = false
    const user = userEvent.setup()
    await renderPage()

    await fillConnectionAndTest(user)

    await waitFor(() => expect(screen.getByText(/连不上 10\.0\.0\.5:3306/)).toBeTruthy())
  })

  it('预览把列名和前几行都摆出来', async () => {
    // 列映射那一步要照着列名配。只有行数的话，用户得自己回数据库里查列名。
    const user = userEvent.setup()
    await renderPage()

    await fillConnectionAndTest(user)
    await waitFor(() =>
      expect((screen.getByRole('button', { name: '下一步' }) as HTMLButtonElement).disabled).toBe(
        false,
      ),
    )
    await user.click(screen.getByRole('button', { name: '下一步' }))
    await user.type(screen.getByLabelText('查询（只允许 SELECT）'), 'SELECT id, name FROM goods')
    await user.click(screen.getByRole('button', { name: '预览' }))

    await waitFor(() => expect(screen.getByText('可乐')).toBeTruthy())
    expect(screen.getByText('id')).toBeTruthy()
    expect(screen.getByText('name')).toBeTruthy()
  })

  async function gotoMapping(user: ReturnType<typeof userEvent.setup>) {
    await fillConnectionAndTest(user)
    await waitFor(() =>
      expect((screen.getByRole('button', { name: '下一步' }) as HTMLButtonElement).disabled).toBe(
        false,
      ),
    )
    await user.click(screen.getByRole('button', { name: '下一步' }))
    await user.type(screen.getByLabelText('查询（只允许 SELECT）'), 'SELECT id, name FROM goods')
    await user.click(screen.getByRole('button', { name: '预览' }))
    await waitFor(() => expect(screen.getByText('可乐')).toBeTruthy())
    await user.click(screen.getByRole('button', { name: '下一步' }))
  }

  it('实体类型从已确认本体里选，不是自由文本', async () => {
    // 填一个本体里没有的类型，ETL 写入时才会被拒（按 status='confirmed'
    // 校验），而那时用户已经配完了整个数据源——连接、SQL、三个列映射全白做。
    // 一个打错的字要走到那么远才报错，等于没有校验。
    confirmedTermTypes = ['产品', '客户']
    const user = userEvent.setup()
    await renderPage()

    await gotoMapping(user)

    const field = await screen.findByLabelText('实体类型')
    expect(field.tagName).toBe('SELECT')
    const options = [...(field as HTMLSelectElement).options].map((o) => o.value)
    expect(options).toContain('产品')
    expect(options).toContain('客户')
    // 第一项是空的占位：不给占位的话浏览器会默认选中第一个真实类型，
    // 用户没选过却像是选了。
    expect(options[0]).toBe('')
  })

  it('拉不到已确认类型时退回自由文本，并说清代价', async () => {
    // 本体服务暂时不可用不该让用户配不了数据源。但也不能默不作声——
    // 用户会以为随便填都行。
    confirmedTermTypes = null
    const user = userEvent.setup()
    await renderPage()

    await gotoMapping(user)

    const field = await screen.findByLabelText('实体类型')
    expect(field.tagName).toBe('INPUT')
    expect(screen.getByText(/导入跑起来时会被拒/)).toBeTruthy()
  })

  it('保存数据源时请求体里没有 password', async () => {
    // 这是本计划面向用户的核心承诺在前端的那一半。后端有 extra="forbid"
    // 兜底，但前端本来就不该发。
    const user = userEvent.setup()
    await renderPage()

    await fillConnectionAndTest(user)
    await waitFor(() =>
      expect((screen.getByRole('button', { name: '下一步' }) as HTMLButtonElement).disabled).toBe(
        false,
      ),
    )
    await user.click(screen.getByRole('button', { name: '下一步' }))
    await user.type(screen.getByLabelText('查询（只允许 SELECT）'), 'SELECT id, name FROM goods')
    await user.click(screen.getByRole('button', { name: '预览' }))
    await waitFor(() => expect(screen.getByText('可乐')).toBeTruthy())
    await user.click(screen.getByRole('button', { name: '下一步' }))
    await user.type(screen.getByLabelText('数据源名字'), '商品库')
    await user.type(screen.getByLabelText('实体类型'), '产品')
    await user.type(screen.getByLabelText('展示名列'), 'name')
    await user.type(screen.getByLabelText('唯一键列'), 'id')
    await user.click(screen.getByRole('button', { name: '保存数据源' }))

    await waitFor(() => {
      const save = requests.find((r) => r.method === 'POST' && r.url.endsWith('/db-import/sources'))
      expect(save).toBeTruthy()
      const body = JSON.parse(save!.body)
      expect(body).not.toHaveProperty('password')
      expect(save!.body).not.toContain('hunter2')
    })
  })

  it('点重新同步会弹密码框，输完才发请求', async () => {
    // 密码不在库里（spec D3）。不弹框直接发的话，同步会带着空密码去连——
    // 空密码在某些配置下真的能连上，那会连到一个错误的账号下。
    const user = userEvent.setup()
    await renderPage()

    await user.click(await screen.findByRole('button', { name: '重新同步' }))

    // 弹框出来之前一个同步请求都不许发。
    expect(requests.filter((r) => r.url.includes('/sync'))).toEqual([])
    await user.type(screen.getByLabelText('密码'), 'hunter2')
    await user.click(screen.getByRole('button', { name: '开始同步' }))

    await waitFor(() =>
      expect(requests.filter((r) => r.url.includes('/sync')).length).toBe(1),
    )
  })

  it('密码框取消时不发同步请求', async () => {
    const user = userEvent.setup()
    await renderPage()

    await user.click(await screen.findByRole('button', { name: '重新同步' }))
    await user.type(screen.getByLabelText('密码'), 'hunter2')
    await user.click(screen.getByRole('button', { name: '取消' }))

    expect(requests.filter((r) => r.url.includes('/sync'))).toEqual([])
  })

  it('密码框里的值不进任何 URL', async () => {
    // URL 会进浏览器历史、进服务端访问日志、进 Referer 头。
    const user = userEvent.setup()
    await renderPage()

    await user.click(await screen.findByRole('button', { name: '重新同步' }))
    await user.type(screen.getByLabelText('密码'), 'hunter2')
    await user.click(screen.getByRole('button', { name: '开始同步' }))

    await waitFor(() => expect(requests.some((r) => r.url.includes('/sync'))).toBe(true))
    expect(requests.every((r) => !r.url.includes('hunter2'))).toBe(true)
    // 也不许留在地址栏里。
    expect(window.location.href).not.toContain('hunter2')
  })

  it('密码交出去之后立刻从密码框自己的 state 里清掉', async () => {
    // 留着的话，这一页只要还开着，密码就一直在内存里躺着，任何一次组件树的
    // dump（错误上报、调试插件）都会带上它。
    //
    // **单测这个组件而不是走整页**：整页上提交之后弹框会被卸载，"再打开是
    // 空的"必然成立——那样的断言在"根本不清"的实现下也照样绿。
    const user = userEvent.setup()
    const submitted: string[] = []
    render(
      <PasswordPrompt
        sourceName="商品库"
        onSubmit={(value) => submitted.push(value)}
        onCancel={() => {}}
      />,
    )

    await user.type(screen.getByLabelText('密码'), 'hunter2')
    await user.click(screen.getByRole('button', { name: '开始同步' }))

    expect(submitted).toEqual(['hunter2'])
    // 组件还挂着，输入框必须已经空了。
    expect((screen.getByLabelText('密码') as HTMLInputElement).value).toBe('')
  })

  it('列表上显示上次同步时间和行数', async () => {
    // 「上次同步 2026-09-08 · 4,712 行」——只有时间的话，用户不知道那次
    // 同步是成功导了数据还是导了个空。
    await renderPage()

    await waitFor(() => expect(screen.getByText(/4,712 行/)).toBeTruthy())
    expect(screen.getByText(/2026-09-08/)).toBeTruthy()
  })

  it('从没同步过的数据源说「还没同步过」，不是显示 0 行', async () => {
    // 「刚刚同步 · 0 行」和「一次都没跑过」是两件完全不同的事。
    sources = [{ ...(sources[0] as object), last_sync_at: null, last_sync_rows: null }]
    await renderPage()

    await waitFor(() => expect(screen.getByText(/还没同步过/)).toBeTruthy())
    expect(screen.queryByText(/0 行/)).toBeNull()
  })

  it('同步失败时把原因说出来', async () => {
    const user = userEvent.setup()
    await renderPage()

    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        requests.push({ url, method: init?.method ?? 'GET', body: String(init?.body ?? '') })
        if (url.includes('/sync')) {
          return Promise.resolve(
            new Response(JSON.stringify({ detail: '连不上 10.0.0.5:3306' }), { status: 400 }),
          )
        }
        if (url.includes('/db-import/sources')) {
          return Promise.resolve(new Response(JSON.stringify({ items: sources }), { status: 200 }))
        }
        return Promise.resolve(new Response(JSON.stringify({}), { status: 200 }))
      }),
    )

    await user.click(await screen.findByRole('button', { name: '重新同步' }))
    await user.type(screen.getByLabelText('密码'), 'hunter2')
    await user.click(screen.getByRole('button', { name: '开始同步' }))

    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('连不上'))
  })

  it('没有数据源时说清楚该做什么，不是一片空白', async () => {
    sources = []
    await renderPage()

    await waitFor(() => expect(screen.getByText(/还没有数据源/)).toBeTruthy())
  })
})
