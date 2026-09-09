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
 * 关系审核的四个分页。
 *
 * 审核员面对这四类要做的事完全不同：模糊匹配是确认一个候选，一端对不上是
 * 给那一端建个实体，不在本体是决定要不要放宽本体，类型非法是改关系类型。
 * 此前它们共用同一对「批准/驳回」按钮混在一屏里，每条都得先判断"这条属于
 * 哪一类"。
 *
 * tab → reason 的映射在后端。前端只传 tab 名——这里的用例因此断言的是
 * **请求里带了哪个 tab**，而不是"过滤出来的内容对不对"（那是后端的事，
 * tests/api/test_admin_graph_review_routes.py 管）。
 */
interface ReviewRow {
  review_id: number
  subject_candidate: string
  object_candidate: string
  relation_type: string
  reason: string
  suggested_subject_standard_name: string | null
  suggested_object_standard_name: string | null
  source: string
  evidence: string | null
  created_at: string
  subject_type_candidate: string | null
  object_type_candidate: string | null
}

function row(id: number, reason: string, subject: string): ReviewRow {
  return {
    review_id: id,
    subject_candidate: subject,
    object_candidate: '认证模块',
    relation_type: 'RELATED_TO',
    reason,
    suggested_subject_standard_name: null,
    suggested_object_standard_name: null,
    source: 's.md',
    evidence: null,
    created_at: '2026-09-08T00:00:00',
    subject_type_candidate: null,
    object_type_candidate: null,
  }
}

/** 每个 tab 各自的返回。key 是 tab 名，`''` 是不带 tab 的那次请求。 */
let byTab: Record<string, ReviewRow[]>
let counts: Record<string, number>
let countsStatus = 200
/** 记下每次列表请求带的 tab，用来断言"点了哪一页就问哪一页"。 */
let requestedTabs: string[] = []
/** 两个就地修复端点收到的请求。 */
let fixRequests: { path: string; body: unknown }[] = []
let fixStatus = 200
let fixDetail = ''

function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
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
      if (url.includes('/graph-reviews/counts')) {
        return Promise.resolve(
          new Response(JSON.stringify(countsStatus === 200 ? counts : {}), {
            status: countsStatus,
          }),
        )
      }
      if (url.includes('/graph-reviews?')) {
        const tab = new URL(url, 'http://x').searchParams.get('tab') ?? ''
        requestedTabs.push(tab)
        const reviews = byTab[tab] ?? []
        return Promise.resolve(
          new Response(JSON.stringify({ reviews, total: reviews.length }), { status: 200 }),
        )
      }
      if (url.includes('/create-missing-term') || url.includes('/allow-combination')) {
        fixRequests.push({ path: url, body: JSON.parse(String(init?.body ?? '{}')) })
        if (fixStatus !== 200) {
          return Promise.resolve(
            new Response(JSON.stringify({ detail: fixDetail }), { status: fixStatus }),
          )
        }
        return Promise.resolve(
          new Response(
            JSON.stringify({
              approved: true,
              combination: '产品 -RELATED_TO-> 模块',
              next_step: '已加进本体草稿。去「本体结构」页确认这份草稿之后，回来批准这条待审。',
            }),
            { status: 200 },
          ),
        )
      }
      if (url.includes('/term-types')) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ term_types: [{ value: '产品' }, { value: '模块' }] }),
            { status: 200 },
          ),
        )
      }
      if (url.includes('/terms')) {
        return Promise.resolve(new Response(JSON.stringify({ terms: [] }), { status: 200 }))
      }
      if (url.includes('/nav-badges')) {
        return Promise.resolve(new Response(JSON.stringify({}), { status: 200 }))
      }
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  byTab = {
    fuzzy: [row(1, 'fuzzy_match_needs_confirmation', '网关超时示例2.0')],
    unresolved: [row(2, 'subject_unresolved', '某个没见过的东西')],
    out_of_ontology: [
      {
        ...row(3, 'not_in_confirmed_ontology', '越界的主语'),
        subject_type_candidate: '产品',
        object_type_candidate: '模块',
      },
    ],
    bad_type: [row(4, 'invalid_relation_type', '类型不对的主语')],
  }
  counts = { fuzzy: 1, unresolved: 7, out_of_ontology: 0, bad_type: 4 }
  countsStatus = 200
  requestedTabs = []
  fixRequests = []
  fixStatus = 200
  fixDetail = ''
  resetAdminSession()
  localStorage.clear()
  stubApi()
})

async function renderReviews() {
  const result = render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[ADMIN_ROUTES.reviewRelations]}>
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
  await screen.findByTestId('admin-topbar')
  return result
}

const tabBar = () => within(screen.getByRole('tablist', { name: '待审理由' }))

describe('关系审核的四个分页', () => {
  it('四个分页都在，默认停在第一个', async () => {
    await renderReviews()
    await waitFor(() => expect(tabBar().getByRole('tab', { name: /模糊匹配/ })).toBeTruthy())
    expect(tabBar().getByRole('tab', { name: /一端对不上/ })).toBeTruthy()
    expect(tabBar().getByRole('tab', { name: /不在本体/ })).toBeTruthy()
    expect(tabBar().getByRole('tab', { name: /类型非法/ })).toBeTruthy()
    expect(
      tabBar().getByRole('tab', { name: /模糊匹配/ }).getAttribute('aria-selected'),
    ).toBe('true')
  })

  it('点另一个分页，请求里带的是那个分页的名字', async () => {
    // 前端只传 tab 名，映射在后端。这条断言的是"点了哪一页就问哪一页"——
    // 内容对不对是后端的事。
    const user = userEvent.setup()
    await renderReviews()
    await waitFor(() => expect(requestedTabs).toContain('fuzzy'))

    await user.click(tabBar().getByRole('tab', { name: /一端对不上/ }))

    await waitFor(() => expect(requestedTabs).toContain('unresolved'))
    // 候选行的文字被拆在多个节点里（"候选：{主语} —[{类型}]→ {宾语}"），
    // 用跨节点的正则匹配整段文本，而不是精确匹配一个文本节点。
    const main = () => within(screen.getByRole('main'))
    await waitFor(() => expect(main().getByText(/某个没见过的东西/)).toBeTruthy())
    // 上一页的条目必须不见了。只断言新的出现的话，「两页都渲染」的实现
    // 照样能绿。
    expect(main().queryByText(/网关超时示例2\.0/)).toBeNull()
  })

  it('每个分页带自己的角标，零不显示', async () => {
    // 角标是这一页有没有东西的唯一提示——不显示的话，审核员得挨个点开
    // 四页才知道哪页有活。而 0 挂在那里只是噪音。
    await renderReviews()
    await waitFor(() => expect(tabBar().getByText('7')).toBeTruthy())
    expect(tabBar().getByText('4')).toBeTruthy()
    // out_of_ontology 是 0，不渲染。
    expect(tabBar().queryByText('0')).toBeNull()
  })

  it('角标拉不到时不显示成 0，也不挡住分页', async () => {
    // 显示 0 是在说"这一页没有待办"，那是一句可能不实的断言——审核员会
    // 据此跳过一页真有东西的分页。拉不到时沉默，但分页本身照常能点。
    countsStatus = 500
    await renderReviews()
    await waitFor(() => expect(tabBar().getByText(/角标没拉到/)).toBeTruthy())
    // 不能编一个 0 出来。而"四个光秃秃的分页"跟"四页都空"长得一模一样，
    // 所以光是不显示还不够——上面那句话才是把这两者分开的东西。
    expect(tabBar().queryByText('0')).toBeNull()
    // 分页本身照常能点：角标是锦上添花，它挂了不该把整页锁住。
    expect(tabBar().getByRole('tab', { name: /一端对不上/ })).toBeTruthy()
  })

  it('某一页空着时说清是这一页空，不是整个队列空', async () => {
    // 「这一类没有待审」和「审核队列是空的」是两件事。说成后者的话，
    // 审核员会以为活干完了，而别的分页里还堆着。
    const user = userEvent.setup()
    byTab.out_of_ontology = []
    await renderReviews()
    await waitFor(() => expect(tabBar().getByRole('tab', { name: /不在本体/ })).toBeTruthy())

    await user.click(tabBar().getByRole('tab', { name: /不在本体/ }))

    await waitFor(() => expect(screen.getByText(/这一类没有待审/)).toBeTruthy())
  })
})

describe('就地修复', () => {
  it('一端对不上那页能就地建实体并批准，名字预填候选名', async () => {
    // 此前审核员得跳到实体明细页建实体、再回来找到这条待审批准。中间隔着
    // 一次导航和一次搜索，而他手上正开着十几条。
    const user = userEvent.setup()
    await renderReviews()
    await user.click(tabBar().getByRole('tab', { name: /一端对不上/ }))
    await waitFor(() => expect(screen.getByRole('button', { name: /新建并批准/ })).toBeTruthy())

    // 名字预填的是管线给的候选名——让审核员从头敲一遍等于让他有机会敲错。
    const nameInput = screen.getByRole('textbox', { name: '要新建的实体名' }) as HTMLInputElement
    expect(nameInput.value).toBe('某个没见过的东西')

    await user.selectOptions(screen.getByRole('combobox', { name: '实体类型' }), '产品')
    await user.click(screen.getByRole('button', { name: /新建并批准/ }))

    await waitFor(() => expect(fixRequests).toHaveLength(1))
    expect(fixRequests[0].path).toContain('/create-missing-term')
    expect(fixRequests[0].body).toEqual({
      standard_name: '某个没见过的东西',
      term_type: '产品',
      side: 'subject',
    })
  })

  it('对不上的是宾语那一端时，side 传的是 object', async () => {
    // reason 决定缺的是哪一端。两端都传 subject 的话，宾语那批会把实体建在
    // 主语位置上——建出来的东西名字对、位置错，而界面看不出来。
    byTab.unresolved = [row(9, 'object_unresolved', '某产品')]
    const user = userEvent.setup()
    await renderReviews()
    await user.click(tabBar().getByRole('tab', { name: /一端对不上/ }))
    await waitFor(() => expect(screen.getByRole('button', { name: /新建并批准/ })).toBeTruthy())

    // 预填的是宾语候选名，不是主语。
    expect(
      (screen.getByRole('textbox', { name: '要新建的实体名' }) as HTMLInputElement).value,
    ).toBe('认证模块')

    await user.selectOptions(screen.getByRole('combobox', { name: '实体类型' }), '模块')
    await user.click(screen.getByRole('button', { name: /新建并批准/ }))

    await waitFor(() => expect(fixRequests).toHaveLength(1))
    expect((fixRequests[0].body as { side: string }).side).toBe('object')
  })

  it('没选类型时按钮点不动，并说清为什么', async () => {
    // 点不动且不说原因，用户会以为界面坏了。
    const user = userEvent.setup()
    await renderReviews()
    await user.click(tabBar().getByRole('tab', { name: /一端对不上/ }))
    await waitFor(() => expect(screen.getByRole('button', { name: /新建并批准/ })).toBeTruthy())

    expect((screen.getByRole('button', { name: /新建并批准/ }) as HTMLButtonElement).disabled).toBe(
      true,
    )
    expect(screen.getByText(/先选一个类型/)).toBeTruthy()
  })

  it('建实体失败时把后端说的原话显示出来，那条待审也还在', async () => {
    // 后端的 400 里点名了是哪个类型不在本体、还列了可以选哪些。包装成
    // 「操作失败」等于把用户唯一能用的信息扔掉。
    fixStatus = 400
    fixDetail = "类型 '产品' 不在这个租户的本体里。先去本体结构页把它建出来"
    const user = userEvent.setup()
    await renderReviews()
    await user.click(tabBar().getByRole('tab', { name: /一端对不上/ }))
    await waitFor(() => expect(screen.getByRole('button', { name: /新建并批准/ })).toBeTruthy())

    await user.selectOptions(screen.getByRole('combobox', { name: '实体类型' }), '产品')
    await user.click(screen.getByRole('button', { name: /新建并批准/ }))

    await waitFor(() => expect(screen.getByText(/不在这个租户的本体里/)).toBeTruthy())
    expect(screen.getByRole('button', { name: /新建并批准/ })).toBeTruthy()
  })

  it('超出本体那页的按钮写出具体组合，且说的是加进草稿不是批准', async () => {
    // 按钮上写「加白名单」的话，审核员不知道自己在放宽什么。
    //
    // 措辞也不能是「并批准」：后端只把组合加进**草稿**，这条待审仍在队列里
    // （加完立刻批准会撞 RelationNotInConfirmedOntologyError，见
    // admin_graph_review_routes.py::allow_combination 的 docstring）。
    // 写成"并批准"就是在界面上承诺一件没发生的事。
    const user = userEvent.setup()
    await renderReviews()
    await user.click(tabBar().getByRole('tab', { name: /不在本体/ }))

    const button = await screen.findByRole('button', { name: /产品.*RELATED_TO.*模块/ })
    expect(button.textContent).not.toContain('批准')
    expect(button.textContent).toContain('草稿')

    await user.click(button)

    await waitFor(() => expect(fixRequests).toHaveLength(1))
    expect(fixRequests[0].path).toContain('/allow-combination')
  })

  it('超出本体那页的批准按钮不按入队时的 reason 写死禁用', async () => {
    // reason 是**入队时**的列，用户按 next_step 去确认了本体草稿之后它
    // 不会变。按它写死禁用的话，用户走完一圈回来按钮还是灰的——而这一页
    // 恰恰刚引导他做了一次影响整租户的本体确认，然后死路。
    //
    // 判据交给后端：approve 端点按**当前**已确认本体校验，不通过时返回的
    // 400 里点名了是哪个组合不在本体里。前端预判一个会过期的状态，只会
    // 在这个状态变化之后说谎。
    byTab.out_of_ontology = [
      {
        ...row(3, 'not_in_confirmed_ontology', '越界的主语'),
        subject_type_candidate: '产品',
        object_type_candidate: '模块',
        suggested_subject_standard_name: '越界的主语',
        suggested_object_standard_name: '认证模块',
      },
    ]
    const user = userEvent.setup()
    await renderReviews()
    await user.click(tabBar().getByRole('tab', { name: /不在本体/ }))

    const approve = await screen.findByRole('button', { name: '批准' })
    expect((approve as HTMLButtonElement).disabled).toBe(false)
  })

  it('加进草稿之后把「还没批准」这件事说出来', async () => {
    // 只弹一句「已加白名单」的话，审核员会以为这条处理完了，而它还挂在
    // 队列里——他下次看到会以为自己上次点了没生效。
    const user = userEvent.setup()
    await renderReviews()
    await user.click(tabBar().getByRole('tab', { name: /不在本体/ }))
    await user.click(await screen.findByRole('button', { name: /产品.*RELATED_TO.*模块/ }))

    await waitFor(() => expect(screen.getByText(/去「本体结构」页确认/)).toBeTruthy())
  })
})
