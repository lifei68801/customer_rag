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
 * 看板：登录后的第一屏，一屏列出这个账号能访问的所有领域。
 *
 * 这一页的设计核心是 spec D5 裁决二——**每个领域一张卡、各自请求、各自
 * 落位**。一个端点返回全部统计的话，第一张卡也要等最慢的那个领域算完，
 * 而每个领域的边计数都是一次图查询。这里最要紧的几条用例全都在钉这件事，
 * 以及它的反面：一张卡失败不能让整页变成错误页。
 */
interface DomainRow {
  tenant_id: string
  name: string
  org_id: string | null
  org_name: string | null
}

const FAST: DomainRow = {
  tenant_id: 'fast',
  name: '商品',
  org_id: 'muji',
  org_name: '无印良品',
}
const SLOW: DomainRow = {
  tenant_id: 'slow',
  name: '门店',
  org_id: 'muji',
  org_name: '无印良品',
}
const LONER: DomainRow = { tenant_id: 'loner', name: '独立库', org_id: null, org_name: null }

function stats(tenantId: string, over: Partial<Record<string, number>> = {}) {
  return {
    tenant_id: tenantId,
    term_count: 20017,
    edge_count: 1204883,
    document_count: 42,
    pending_review_count: 7,
    sheet_row_count: 4712,
    stale_question_count: 0,
    ...over,
  }
}

let domainsBody: { domains: DomainRow[] }
let domainsStatus = 200
/** 每个租户各自的 stats 响应。返回 Promise 好让用例控制先后。 */
let statsResponders: Record<string, () => Promise<Response>>
let switchRequests: string[] = []
/** whoami 回的当前租户。第 9 项人工核查（admin 没切过租户）要把它设成 null。 */
let currentTenantId: string | null = 'fast'
/** 切租户的 PUT 什么时候完成——竞态那条用例要卡住它。 */
let resolveSwitch: (() => void) | null = null

function jsonResponse(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status }))
}

function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/auth/whoami')) {
        return jsonResponse({
          username: 'alice',
          role: 'member',
          tenant_id: 'fast',
          current_tenant_id: currentTenantId,
        })
      }
      if (url.includes('/api/admin/auth/session/tenant')) {
        switchRequests.push(JSON.parse(String(init?.body ?? '{}')).tenant_id as string)
        const body = JSON.parse(String(init?.body ?? '{}'))
        if (resolveSwitch === null) return jsonResponse({ tenant_id: body.tenant_id })
        return new Promise<Response>((resolve) => {
          resolveSwitch = () => resolve(new Response(JSON.stringify(body), { status: 200 }))
        })
      }
      if (url.includes('/api/admin/dashboard/domains')) {
        return jsonResponse(domainsBody, domainsStatus)
      }
      // 逐领域统计。租户 id 在路径中段：/api/admin/{tenant}/dashboard/stats
      const m = /\/api\/admin\/([^/]+)\/dashboard\/stats$/.exec(url)
      if (m) {
        const responder = statsResponders[decodeURIComponent(m[1])]
        return responder ? responder() : jsonResponse({}, 404)
      }
      if (url.includes('/nav-badges')) return jsonResponse({})
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  domainsBody = { domains: [FAST, SLOW, LONER] }
  domainsStatus = 200
  statsResponders = {
    fast: () => jsonResponse(stats('fast')),
    slow: () => jsonResponse(stats('slow', { term_count: 1 })),
    loner: () => jsonResponse(stats('loner', { term_count: 3 })),
  }
  switchRequests = []
  currentTenantId = 'fast'
  resolveSwitch = null
  resetAdminSession()
  localStorage.clear()
  stubApi()
})

async function renderDashboard() {
  const result = render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[ADMIN_ROUTES.dashboard]}>
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
  await screen.findByTestId('admin-topbar')
  return result
}

const card = (tenantId: string) => within(screen.getByTestId(`domain-card-${tenantId}`))

describe('看板', () => {
  it('每张卡各自请求各自落位——快的先出来，不等慢的', async () => {
    // spec D5 裁决二。一个端点返回全部统计、或者前端等 Promise.all 再一次性
    // 渲染的话，第一张卡也要等最慢的那个领域算完。
    let releaseSlow: (() => void) | null = null
    statsResponders.slow = () =>
      new Promise<Response>((resolve) => {
        releaseSlow = () => resolve(new Response(JSON.stringify(stats('slow')), { status: 200 }))
      })

    await renderDashboard()

    // fast 那张卡已经落了数字，slow 那张还在骨架屏。
    await waitFor(() => expect(card('fast').getByText('20,017')).toBeTruthy())
    expect(screen.getByTestId('domain-card-slow-skeleton')).toBeTruthy()

    // 慢的那个回来之后它自己也落位，快的那张不受影响。
    releaseSlow!()
    await waitFor(() => expect(card('slow').getByText('20,017')).toBeTruthy())
    expect(card('fast').getByText('20,017')).toBeTruthy()
  })

  it('骨架屏先出，不是白屏', async () => {
    // 清单已经回来了，统计还没有。这一刻领域名和卡片轮廓就该在了——
    // 一屏空白读起来像页面没加载出来。
    statsResponders.fast = () => new Promise<Response>(() => {})
    statsResponders.slow = () => new Promise<Response>(() => {})
    statsResponders.loner = () => new Promise<Response>(() => {})

    await renderDashboard()

    await waitFor(() => expect(screen.getByText('商品')).toBeTruthy())
    expect(screen.getByTestId('domain-card-fast-skeleton')).toBeTruthy()
    expect(screen.getByText('门店')).toBeTruthy()
  })

  it('一张卡统计失败时只有它显示失败，别的卡照常', async () => {
    // 一个领域的图谱查不通，不该让整个看板变成一个错误页——别的领域的
    // 数字是好的，凭什么一起看不到。
    statsResponders.slow = () => jsonResponse({ detail: '统计没算出来' }, 503)

    await renderDashboard()

    await waitFor(() => expect(card('slow').getByText(/统计没算出来/)).toBeTruthy())
    expect(card('fast').getByText('20,017')).toBeTruthy()
    // 失败的那张要给得出重试，只说坏了不给出路等于只做了一半。
    expect(card('slow').getByRole('button', { name: '重试' })).toBeTruthy()
  })

  it('数字带千分位', async () => {
    // 1204883 读不出来是一百二十万还是十二万。
    await renderDashboard()
    await waitFor(() => expect(card('fast').getByText('1,204,883')).toBeTruthy())
  })

  it('按组织分组，没挂组织的单独一组', async () => {
    // 没挂组织是合法状态（存量租户），不是错误。
    await renderDashboard()
    await waitFor(() => expect(screen.getByText('无印良品')).toBeTruthy())
    const muji = within(screen.getByTestId('org-group-muji'))
    expect(muji.getByTestId('domain-card-fast')).toBeTruthy()
    expect(muji.getByTestId('domain-card-slow')).toBeTruthy()
    // 独立库不能混进那一组里——混进去等于谎报它的归属。
    expect(muji.queryByTestId('domain-card-loner')).toBeNull()
    expect(within(screen.getByTestId('org-group-none')).getByTestId('domain-card-loner')).toBeTruthy()
  })

  it('待办数不为零时能点，点了先切到那个领域再去审核页', async () => {
    // 看板的价值在于「看到之后能立刻去做」。而看板是跨领域的：当前挂着
    // fast，点的是 slow 那张卡上的待办。不先把当前租户切过去就跳，用户
    // 落在审核页上看到的是 fast 的队列——数字是 slow 的、内容是 fast 的。
    const user = userEvent.setup()
    await renderDashboard()
    await waitFor(() => expect(card('slow').getByRole('button', { name: /7/ })).toBeTruthy())

    // 卡住切租户的 PUT。要断言的是"切成功之前**不跳**"——只断言"最后跳
    // 到了"的话，`void switchTenant(); navigate()` 这个竞态实现照样能绿
    // （变异 E 验过：第一版就是这么写的，它没被打红）。
    resolveSwitch = () => {}
    await user.click(card('slow').getByRole('button', { name: /7/ }))
    await waitFor(() => expect(switchRequests).toEqual(['slow']))
    // 还停在看板上：卡片还在。断言看板上的东西还在，而不是断言目标页
    // 还没出现——后者在目标页没有可查的标识时是一句永真的空话。
    expect(screen.getByTestId('domain-card-slow')).toBeTruthy()

    resolveSwitch!()
    await waitFor(() => expect(screen.queryByTestId('domain-card-slow')).toBeNull())
  })

  it('卡片上有「表格行数」', async () => {
    // spec §5 卡片的第四格。少了它用户分不清 2 万个实体里多少是表格导进来的、
    // 多少是文档抽出来的——而这两条路径的修法完全不同。
    await renderDashboard()
    await waitFor(() => expect(card('fast').getByText('4,712')).toBeTruthy())
    expect(card('fast').getByText('表格行')).toBeTruthy()
  })

  it('有失效的引导问题时卡片上出现一条待办并能跳到数字人页', async () => {
    // spec 前台硬规矩之二：失效了必须有人知道。只在数字人配置页能看见的话，
    // 要用户主动去翻——那正是「默默消失」。
    statsResponders.fast = () => jsonResponse(stats('fast', { stale_question_count: 2 }))
    const user = userEvent.setup()
    await renderDashboard()

    const todo = await waitFor(() => card('fast').getByRole('button', { name: /2 条引导问题失效/ }))
    resolveSwitch = () => {}
    await user.click(todo)
    await waitFor(() => expect(switchRequests).toEqual(['fast']))
  })

  it('没有失效问题时那条待办不出现', async () => {
    // 恒显示的话用户很快就不看它了，而它在真的失效时是关键信息。
    await renderDashboard()
    await waitFor(() => expect(card('fast').getByText('20,017')).toBeTruthy())
    expect(card('fast').queryByRole('button', { name: /引导问题失效/ })).toBeNull()
  })

  it('待办为零的领域不给一个点了没用的入口', async () => {
    // 0 不是一件等着你做的事。给它一个链接，点进去是一个空队列。
    statsResponders.fast = () => jsonResponse(stats('fast', { pending_review_count: 0 }))
    await renderDashboard()
    await waitFor(() => expect(card('fast').getByText('20,017')).toBeTruthy())
    expect(card('fast').queryByRole('button', { name: /待审/ })).toBeNull()
  })

  it('一条数据都没有的领域，指出下一步而不是摆四个零', async () => {
    // 这条接的是导航重排删掉 AdminLanding 时留下的账：那个落地分流原本
    // 负责把"还没建本体"的新租户送去本体结构页。现在这条引导归看板——
    // 四个 0 只说明"这里是空的"，不说明该干什么。
    statsResponders.loner = () =>
      jsonResponse(
        stats('loner', {
          term_count: 0,
          edge_count: 0,
          document_count: 0,
          pending_review_count: 0,
        }),
      )
    await renderDashboard()
    await waitFor(() => expect(card('loner').getByText(/还没有数据/)).toBeTruthy())
    expect(card('loner').getByRole('button', { name: /导入|建本体/ })).toBeTruthy()
  })

  it('还没选过租户的账号打开看板，看到的是看板不是「请先选择一个租户」', async () => {
    // 看板是登录后的落地页，而 admin 的 admin_users.tenant_id 恒为 None
    // ——他没切过租户之前 current_tenant_id 就是 null。归成租户内路由的话，
    // 新登录的 admin 第一眼看到的是一屏空态，而看板恰恰是**跨领域**的，
    // 它一屏列出所有领域，本来就不属于其中任何一个。
    //
    // adminRoutes.test.ts 里那条只测了 routeRequiresTenant 的返回值；
    // 这一条走整个 App，把 AdminLayout 那道闸门也一起钉住。
    currentTenantId = null
    await renderDashboard()
    await waitFor(() => expect(screen.getByTestId('domain-card-fast')).toBeTruthy())
    expect(screen.queryByText('请先选择一个租户')).toBeNull()
  })

  it('访问 /admin 本身也落到看板，不被「请先选择一个租户」挡住', async () => {
    // /admin 是落地跳转的一站，它自己不渲染任何内容。而 AdminLayout 的
    // 租户闸门在 <Outlet/> 之外判定：routeRequiresTenant('/admin') 为 true
    // 的话，那条 <Route index> 的 <Navigate> 根本没机会渲染，用户卡在空态。
    // admin 的 current_tenant_id 恒为 None，所以这条路径上每一个新 admin
    // 账号、每一次后端重启后重新登录，第一屏都是空态。
    currentTenantId = null
    render(
      <SkinProvider>
        <ConfirmProvider>
          <ToastProvider>
            <MemoryRouter initialEntries={['/admin']}>
              <App />
            </MemoryRouter>
          </ToastProvider>
        </ConfirmProvider>
      </SkinProvider>,
    )
    await screen.findByTestId('admin-topbar')
    await waitFor(() => expect(screen.getByTestId('domain-card-fast')).toBeTruthy())
    expect(screen.queryByText('请先选择一个租户')).toBeNull()
  })

  it('传了文档但一个实体都没抽出来的领域，也给得出下一步', async () => {
    // 这一档是真会踩的：本体没确认时文档管线会跳过图谱抽取
    // （ingestion/pipeline.py），所以「文档 3、实体 0、关系 0、待审 0」是
    // 一个常见状态。它不满足"四个数字全为 0"，落进 else 分支，而待审又是
    // 0——那张卡上一个按钮都没有，用户只看到三个 0 和一个 3，不知道该干嘛。
    //
    // 这正是导航重排删掉 AdminLanding 之后要由看板接住的那条引导：那个
    // 落地分流原本就是按"本体确认了没有"分的。
    statsResponders.loner = () =>
      jsonResponse(
        stats('loner', {
          term_count: 0,
          edge_count: 0,
          document_count: 3,
          pending_review_count: 0,
        }),
      )
    await renderDashboard()
    await waitFor(() => expect(card('loner').getByText('3')).toBeTruthy())
    expect(card('loner').getByRole('button', { name: /本体|建模/ })).toBeTruthy()
  })

  it('一个领域都没有时说清楚该做什么', async () => {
    // 新部署的第一屏。「暂无数据」等于什么都没说。
    domainsBody = { domains: [] }
    await renderDashboard()
    await waitFor(() => expect(screen.getByText(/还没有任何领域/)).toBeTruthy())
  })

  it('清单本身拉不到时说出来，不是显示成「一个领域都没有」', async () => {
    // 「你没有任何领域」和「清单没拉回来」在界面上长得一样，而前者会让
    // 用户去找管理员要授权，后者该报修。
    domainsStatus = 500
    await renderDashboard()
    await waitFor(() => expect(screen.getByText(/领域清单加载失败/)).toBeTruthy())
    expect(screen.queryByText(/还没有任何领域/)).toBeNull()
  })
})
