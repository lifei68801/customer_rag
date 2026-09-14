import { beforeEach, describe, expect, it } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { ADMIN_ROUTES } from '../adminRoutes'
import type { EtlMapping, EtlMappingSummary } from './etlMappingApi'
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
 * 表格导入页首屏：本体是否已经带着引导配好的映射，决定用户看到的是
 * 「传数据文件即可」还是「从头配置」。这是 admin-flow-continuity 计划的
 * 第三个任务——前两个任务已经让映射与本体同生命周期存储、并提供了
 * fetchEtlMapping 读取接口，本任务是第一个消费方。
 */

/** 后端说的「草稿里的实体类型跟已确认的不是同一批」。 */
let hasUnconfirmedChanges = false
let etlMappingStubbed = false
let etlMappingValue: EtlMapping | null = null
let runsListResponse: { run_id: string; status: string; started_at: string; finished_at: string | null }[] = []
let runDetailResponse: Record<string, unknown> | null = null
let requests: { url: string; init?: RequestInit }[] = []
/** 表格导入页的映射编辑器只认已确认的实体类型/允许组合。 */
let confirmedTermTypes: { value: string; extra_fields: { name: string; value_type: string; label?: string }[] }[] = []
let confirmedCombinations: { subject_term_type: string; relation_type: string; object_term_type: string }[] = []

function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/auth/whoami')) return whoamiResponse()
      requests.push({ url, init })
      const json = (body: unknown, status = 200) =>
        Promise.resolve(new Response(JSON.stringify(body), { status }))
      if (url.includes('/term-types')) {
        return json({ term_types: confirmedTermTypes })
      }
      if (url.includes('/constraints')) {
        return json({ constraints: confirmedCombinations })
      }
      if (url.includes('/nav-badges')) {
        return json({ pending_relations: 0, pending_duplicates: 0, total_terms: 0 })
      }
      if (url.includes('/schema-etl/status')) {
        return json({
          ontology_confirmed: true,
          has_unconfirmed_term_type_changes: hasUnconfirmedChanges,
        })
      }
      if (/\/promote$/.test(url)) {
        return json({ run_id: 'promoted-run' })
      }
      if (/\/schema-etl\/runs\/[^/]+$/.test(url)) {
        return json(runDetailResponse ?? {})
      }
      if (url.includes('/schema-etl/runs') && init?.method === 'POST') {
        // 跟真实 ETL 一样：提交之后这条跑批先是 running，跑完才变 failed。
        // 页面在提交那一刻拿到的详情是 running 的，后面得靠轮询才知道它
        // 失败了——桩直接给 failed 的话，"详情从不刷新"这个缺陷测不出来。
        if (startRunOutcome) {
          const running: RunRow = { ...startRunOutcome.listRow, status: 'running', finished_at: null }
          runsListResponse = [running, ...runsListResponse]
          runDetailResponse = { ...startRunOutcome.detail, status: 'running', error: null, finished_at: null }
          settleRun = () => {
            runsListResponse = [startRunOutcome!.listRow, ...runsListResponse.slice(1)]
            runDetailResponse = startRunOutcome!.detail
          }
        }
        // 跟后端契约一致：只有"带着 config 的真跑"才会把这份映射记成默认
        // （没传 config 就是照旧跑，预演不改任何持久状态）。桩不照这个来的话，
        // 这条提示会在它根本不会出现的场景里被验成绿的。
        const body = init?.body as FormData | undefined
        const savedAsDefault = body instanceof FormData
          ? body.has('config') && body.get('dry_run') !== 'true'
          : false
        return json({
          run_id: startRunOutcome?.listRow.run_id ?? 'run-new',
          mapping_saved_as_default: savedAsDefault,
        })
      }
      if (url.includes('/schema-etl/runs')) {
        return json({ runs: runsListResponse })
      }
      if (url.includes('/schema-etl/sample')) {
        return json({ files: [] })
      }
      // /etl-mapping 只有测试显式调用 stubEtlMapping 之后才会 resolve——
      // 没调用就落进下面的 catch-all，永不 resolve，用来模拟「未知态」。
      if (etlMappingStubbed && url.includes('/etl-mapping')) {
        return json({ mapping: etlMappingValue })
      }
      return new Promise(() => {})
    }),
  )
}

/** 提交跑批之后它变成什么样：列表里的那一行 + 点开的详情。 */
type RunRow = { run_id: string; status: string; started_at: string; finished_at: string | null }
let startRunOutcome: { listRow: RunRow; detail: Record<string, unknown> } | null = null
/** 让那条 running 的跑批"跑完"——之后列表和详情就返回最终状态。 */
let settleRun: (() => void) | null = null

function stubEtlMapping(mapping: EtlMapping | null) {
  etlMappingStubbed = true
  etlMappingValue = mapping
}

// stubCompletedRun（forwardLinks.test.tsx）的同款写法：一条已完成跑批，
// dry_run/status 由调用方指定，跑批详情要点了 run_id 那一行才会挂载。
function stubSelectedRun(report: { dry_run: boolean; status: string }) {
  runsListResponse = [
    { run_id: 'run-1', status: report.status, started_at: '2026-09-03T00:00:00', finished_at: '2026-09-03T00:05:00' },
  ]
  runDetailResponse = {
    run_id: 'run-1',
    status: report.status,
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

// 最近一次「点了按钮才会发生」的请求——不是字面意义的最后一次 fetch。
// 正式执行成功后会立刻踢一次轮询（pollNowRef），那个 GET /runs 紧跟着
// POST /promote 发生，字面上的"最后一次"会变成轮询请求，测试真正想问的
// 是"点击触发的是哪个写操作"，所以只看非 GET 请求里最新的一条。
function lastRequest() {
  return [...requests].reverse().find((r) => (r.init?.method ?? 'GET') !== 'GET')
}

beforeEach(() => {
  signedInRole = null
  resetAdminSession()
  sessionStorage.clear()
  localStorage.clear()
  hasUnconfirmedChanges = false
  etlMappingStubbed = false
  etlMappingValue = null
  runsListResponse = []
  runDetailResponse = null
  requests = []
  startRunOutcome = null
  settleRun = null
  confirmedTermTypes = []
  confirmedCombinations = []
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

/** demo 那份映射：订单挂着几个度量，客户名下挂着邮编（正是那次事故的形状）。 */
const DEMO_SUMMARY: EtlMappingSummary = {
  entities: [
    {
      term_type: 'Order ID',
      source_file: 'soft_drink_sales.xlsx',
      key_columns: ['Order ID'],
      key_parts: [{ kind: 'column' as const, column: 'Order ID' }],
      name_columns: ['Order ID'],
      attributes: { Revenue: 'Revenue' },
    },
    {
      term_type: 'Customer Name',
      source_file: 'soft_drink_sales.xlsx',
      key_columns: ['Customer Name'],
      key_parts: [{ kind: 'column' as const, column: 'Customer Name' }],
      name_columns: ['Customer Name'],
      attributes: { Customer_Zip_Code: 'Customer Zip Code' },
    },
  ],
  relations: [
    {
      relation_type: 'HAS_CUSTOMER_NAME',
      subject_term_type: 'Order ID',
      object_term_type: 'Customer Name',
    },
  ],
}

const DEMO_HEADER = 'Order ID,Revenue,Customer Name,Customer Zip Code'

function stubDemoMapping(summary: EtlMappingSummary | null = DEMO_SUMMARY) {
  stubEtlMapping({
    config_yaml: 'entities: []',
    source_file_name: 'soft_drink_sales.xlsx',
    created_at: '2026-09-11T00:00:00',
    summary,
  })
}

function csv(header: string, name = 'sales_2026.csv') {
  return new File([`${header}\nA,1,张三,100000\n`], name, { type: 'text/csv' })
}

/** 选文件 → 等第二步填好。返回流程容器。 */
async function chooseFile(user: ReturnType<typeof userEvent.setup>, file: File) {
  const flow = await screen.findByTestId('table-import-flow')
  await user.upload(within(flow).getByLabelText(/数据文件/) as HTMLInputElement, file)
  await screen.findByTestId('mapping-overview')
  return flow
}

describe('表格导入的三步流程', () => {
  it('第一步就是选数据文件，不用先点开任何折叠面板', async () => {
    // 此前主区域给的是运行按钮，映射折在下面的面板里，而那个面板里还要
    // 再传一次表——用户点运行时根本没见过映射。
    signIn('admin')
    stubDemoMapping()
    renderAt(ADMIN_ROUTES.etl)

    const flow = await screen.findByTestId('table-import-flow')
    expect(within(flow).getByLabelText(/数据文件/)).toBeTruthy()
    // 页面上只有这一个主按钮。此前「开始运行」和「确认并开始运行」并列，
    // 做的事不同而外观一致。
    expect(screen.getAllByRole('button', { name: /^开始导入$/ })).toHaveLength(1)
  })

  it('选完文件，第二步直接摆出哪列是身份键、哪列挂在哪个实体下', async () => {
    signIn('admin')
    stubDemoMapping()
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.etl)

    const flow = await chooseFile(user, csv(DEMO_HEADER))

    const overview = within(flow).getByTestId('mapping-overview')
    expect(overview.textContent).toMatch(/Customer Name/)
    expect(overview.textContent).toMatch(/Order ID —HAS_CUSTOMER_NAME→ Customer Name/)
    expect(within(flow).getByText(/沿用上次配好的映射/)).toBeTruthy()
  })

  it('点「改这份映射」就能改，邮编挂在客户名下这件事在编辑器里看得见', async () => {
    // 事故的形状：邮编挂在「客户名」这个身份键下，同名客户邮编不同，ETL
    // 拒绝写入。用户至少要够得着那一处才改得动。
    signIn('admin')
    stubDemoMapping()
    confirmedTermTypes = [
      { value: 'Order ID', extra_fields: [{ name: 'Revenue', value_type: 'integer', label: 'Revenue' }] },
      {
        value: 'Customer Name',
        extra_fields: [
          { name: 'Customer_Zip_Code', value_type: 'integer', label: 'Customer Zip Code' },
        ],
      },
    ]
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.etl)

    const flow = await chooseFile(user, csv(DEMO_HEADER))
    await user.click(within(flow).getByTestId('toggle-mapping-editor'))

    expect(within(flow).queryByTestId('mapping-overview')).toBeNull()
    // 按 testid 找，不按文字：'Customer Zip Code' 同时也是每个下拉里的一个
    // <option>，按文字找会撞上一堆。
    const zip = await within(flow).findByTestId('field-mapping-Customer_Zip_Code')
    expect((zip.querySelector('select') as HTMLSelectElement).value).toBe('Customer Zip Code')
  })

  it('邮编挂在客户名下、同名客户邮编不同 → 点运行之前就报出来，并给出路', async () => {
    // 这条用例复现的就是那次事故：跑批失败后甩出"530 个 node_key 被算出了
    // 不同的值"，而界面上没有任何地方能改那件事。现在它出现在运行按钮之前。
    signIn('admin')
    stubDemoMapping()
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.etl)

    const flow = await screen.findByTestId('table-import-flow')
    await user.upload(
      within(flow).getByLabelText(/数据文件/) as HTMLInputElement,
      // 两个「张三」，邮编不同。
      new File(
        [`${DEMO_HEADER}
O1,1,张三,100000
O2,2,张三,200000
`],
        'sales_2026.csv',
        { type: 'text/csv' },
      ),
    )

    const notice = await within(flow).findByTestId('mapping-conflict-Customer_Zip_Code')
    expect(notice.textContent).toMatch(/张三/)
    expect(notice.textContent).toMatch(/100000/)

    // 只报问题不给出路的话，用户能做的只有重传同一个文件再失败一次。
    await user.click(within(notice).getByRole('button', { name: /也算进身份键/ }))
    await waitFor(() =>
      expect(within(flow).queryByTestId('mapping-conflict-Customer_Zip_Code')).toBeNull(),
    )
  }, 20000)

  it('邮编挂在订单号下就不报——订单号每行一个', async () => {
    // 会误报的预检等于没有预检：用户学会忽略它之后，真出问题那次也会被忽略。
    signIn('admin')
    stubDemoMapping({
      entities: [
        {
          term_type: 'Order ID',
          source_file: 'soft_drink_sales.xlsx',
          key_columns: ['Order ID'],
          key_parts: [{ kind: 'column', column: 'Order ID' }],
          name_columns: ['Order ID'],
          attributes: { Customer_Zip_Code: 'Customer Zip Code' },
        },
      ],
      relations: [],
    })
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.etl)

    const flow = await screen.findByTestId('table-import-flow')
    await user.upload(
      within(flow).getByLabelText(/数据文件/) as HTMLInputElement,
      new File(
        [`${DEMO_HEADER}
O1,1,张三,100000
O2,2,张三,200000
`],
        'sales_2026.csv',
        { type: 'text/csv' },
      ),
    )

    await screen.findByTestId('mapping-overview')
    await waitFor(() =>
      expect(within(flow).queryByText(/正在检查这份映射跑不跑得通/)).toBeNull(),
    )
    expect(within(flow).queryByTestId('mapping-conflict-Customer_Zip_Code')).toBeNull()
  }, 20000)

  it('沿用存好的映射、一个字没改时，提交不带 config', async () => {
    // 带上一份 config 的话后端会拿它当权威，而这里生成的那份未必跟存着的
    // 一模一样（比如存的是多列拼接的展示名，编辑器只放得下一列）。
    signIn('admin')
    stubDemoMapping()
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.etl)

    const flow = await chooseFile(user, csv(DEMO_HEADER))
    await user.click(within(flow).getByTestId('run-import'))

    await waitFor(() => {
      const posted = requests.find(
        (r) => r.url.includes('/schema-etl/runs') && r.init?.method === 'POST',
      )
      expect(posted).toBeTruthy()
      const body = posted!.init!.body as FormData
      expect(body.has('config')).toBe(false)
      expect(body.getAll('data_files')).toHaveLength(1)
    })
  })

  it('改过映射再跑时说一句"已存为默认"——它改变了下次进这一页的默认行为', async () => {
    signIn('admin')
    stubDemoMapping()
    confirmedTermTypes = [
      { value: 'Order ID', extra_fields: [] },
      { value: 'Customer Name', extra_fields: [] },
    ]
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.etl)

    const flow = await chooseFile(user, csv(DEMO_HEADER))
    await user.click(within(flow).getByTestId('toggle-mapping-editor'))
    const removeButtons = await within(flow).findAllByRole('button', { name: '删除' })
    await user.click(removeButtons[1])
    await user.click(within(flow).getByTestId('run-import'))

    expect(await screen.findByText(/这份映射已存为默认/)).toBeTruthy()
  })

  it('照旧跑（没改映射）时不说这句话——那一次后端不会改默认映射', async () => {
    signIn('admin')
    stubDemoMapping()
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.etl)

    const flow = await chooseFile(user, csv(DEMO_HEADER))
    await user.click(within(flow).getByTestId('run-import'))

    expect(await screen.findByText('已提交运行')).toBeTruthy()
    expect(screen.queryByText(/已存为默认/)).toBeNull()
  })

  it('改过映射之后，提交就带上改完的那份', async () => {
    // 不带的话后端会用存着的旧映射跑——用户刚做的修改被静默丢弃，而界面上
    // 看不出发生过这件事。
    signIn('admin')
    stubDemoMapping()
    confirmedTermTypes = [
      { value: 'Order ID', extra_fields: [] },
      { value: 'Customer Name', extra_fields: [] },
    ]
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.etl)

    const flow = await chooseFile(user, csv(DEMO_HEADER))
    await user.click(within(flow).getByTestId('toggle-mapping-editor'))
    const removeButtons = await within(flow).findAllByRole('button', { name: '删除' })
    await user.click(removeButtons[1])
    await user.click(within(flow).getByTestId('run-import'))

    await waitFor(() => {
      const posted = requests.find(
        (r) => r.url.includes('/schema-etl/runs') && r.init?.method === 'POST',
      )
      expect(posted).toBeTruthy()
      expect((posted!.init!.body as FormData).has('config')).toBe(true)
    })
  })

  it('存好的映射用到的列这张表没有时，说出缺哪列，并改用按本体推的建议', async () => {
    // 不给原因的话，用户看到的是"这不是我上次配的那份"，最可能的猜测是
    // 系统把配置弄丢了；真实原因通常是他换了一张列名不同的表。
    signIn('admin')
    stubDemoMapping()
    confirmedTermTypes = [{ value: 'Order ID', extra_fields: [] }]
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.etl)

    const flow = await screen.findByTestId('table-import-flow')
    await user.upload(
      within(flow).getByLabelText(/数据文件/) as HTMLInputElement,
      csv('Order ID,Revenue'),
    )

    const notice = await within(flow).findByTestId('stored-mapping-not-reused')
    expect(notice.textContent).toMatch(/Customer Name/)
    expect(within(flow).getByText(/按已确认本体现推的建议/)).toBeTruthy()
  })

  it('一个文件都没选时运行按钮不可点，也不会发出一次空请求', async () => {
    signIn('admin')
    stubDemoMapping()
    renderAt(ADMIN_ROUTES.etl)

    const flow = await screen.findByTestId('table-import-flow')
    expect(within(flow).getByTestId('run-import').hasAttribute('disabled')).toBe(true)
    expect(
      requests.find((r) => r.url.includes('/schema-etl/runs') && r.init?.method === 'POST'),
    ).toBeUndefined()
  })

  it('没有存过映射时，走的是同一条流程，只是第二步给的是建议', async () => {
    // 此前无映射走的是另一套界面（构建器默认展开当主角），有映射走运行
    // 表单。两条路并存正是"入口多、找不到"的来源。
    signIn('admin')
    stubEtlMapping(null)
    confirmedTermTypes = [{ value: 'Order ID', extra_fields: [] }]
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.etl)

    const flow = await screen.findByTestId('table-import-flow')
    expect(screen.queryByText(/已经配好了一份映射/)).toBeNull()
    await user.upload(
      within(flow).getByLabelText(/数据文件/) as HTMLInputElement,
      csv('Order ID,Revenue'),
    )
    // 没看过的建议默认展开，不用先自己发现「这里能点开」。
    expect(await within(flow).findByText(/按已确认本体现推的建议/)).toBeTruthy()
    expect(within(flow).queryByTestId('mapping-overview')).toBeNull()
  })

  it('提交后跑批失败，原因不用点任何东西就能看见，旁边给「去改映射」', async () => {
    signIn('admin')
    stubDemoMapping()
    confirmedTermTypes = [{ value: 'Order ID', extra_fields: [] }]
    startRunOutcome = {
      listRow: {
        run_id: 'run-new',
        status: 'failed',
        started_at: '2026-09-13T00:00:00',
        finished_at: '2026-09-13T00:00:05',
      },
      detail: {
        run_id: 'run-new',
        status: 'failed',
        started_at: '2026-09-13T00:00:00',
        finished_at: '2026-09-13T00:00:05',
        error: "实体类型 'Customer Name' 有 530 个 node_key 被算出了不同的值",
        report: null,
      },
    }
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.etl)

    const flow = await chooseFile(user, csv(DEMO_HEADER))
    await user.click(within(flow).getByTestId('run-import'))

    // 提交那一刻它还在跑。详情已经开着，但没有失败原因可显示。
    await screen.findByText(/跑批详情：run-new/)
    expect(screen.queryByRole('alert')).toBeNull()

    // ETL 跑完了、失败了。页面自己轮询列表，详情得跟着刷新——不点任何一行，
    // 原因就得出现在页面上。
    settleRun!()
    const alert = await screen.findByRole('alert', {}, { timeout: 8000 })
    expect(alert.textContent).toContain('530 个 node_key')

    // 原因十有八九在映射上，不在文件上。点「去改映射」要把第二步的编辑器
    // 展开——只报原因不给出路，用户会去重新上传同一个文件再失败一次。
    await user.click(screen.getByTestId('fix-mapping-from-failure'))
    await waitFor(() => expect(within(flow).queryByTestId('mapping-overview')).toBeNull())
  }, 20000)
})

describe('本体草稿未确认的提示', () => {
  it('草稿和已确认不是同一批时说出来，并指路去确认', async () => {
    // 引导建模落下来的是**草稿**，而这一页的实体类型下拉读的是**已确认**那批。
    // 两者不一致时不说的话，用户会把这张表的列映射到另一份数据集的实体类型上
    // ——ETL 要么"成功"产出垃圾，要么在跑起来之后才以一个指不到根因的方式失败，
    // 而屏幕上一切正常。
    hasUnconfirmedChanges = true
    signIn('admin')
    renderAt(ADMIN_ROUTES.etl)
    const notice = await screen.findByTestId('unconfirmed-draft-notice')
    // 要说清"下拉里是旧的那批"，只说"有未确认草稿"用户不知道它跟眼前这个
    // 下拉有什么关系。
    expect(notice.textContent).toMatch(/草稿/)
    expect(notice.textContent).toMatch(/已确认|生效/)
    // 给一个能点过去的入口，不是让他自己找。
    expect(within(notice).getByRole('link')).toBeTruthy()
  })

  it('草稿和已确认一致时不出这条提示', async () => {
    // 会误报的提示等于没有提示。只是打开一眼本体页（checkout 会复制一份
    // 一模一样的草稿）不该触发它——判据在后端，这里钉住前端如实转达。
    hasUnconfirmedChanges = false
    signIn('admin')
    renderAt(ADMIN_ROUTES.etl)
    await screen.findByTestId('admin-topbar')
    expect(screen.queryByTestId('unconfirmed-draft-notice')).toBeNull()
  })
})

describe('表格导入页首屏', () => {
  it('已经配好映射时说一句就够，映射长什么样留到第二步', async () => {
    // 刚走完引导的用户看到一个邀请他从头配置的界面，等于让他重做刚做完的
    // 工作。这里只说"有一份、来自哪张表"——它的内容在第二步展示，两处都画
    // 一遍的话，一旦不一致用户不知道该信哪个。
    signIn('admin')
    stubDemoMapping()
    renderAt(ADMIN_ROUTES.etl)

    expect(await screen.findByText(/已经配好了一份映射/)).toBeTruthy()
    expect(screen.getByText('soft_drink_sales.xlsx')).toBeTruthy()
    // 还没选文件，第二步没有可展示的映射。
    expect(screen.queryByTestId('mapping-overview')).toBeNull()
  })

  it('没有映射时也是同一条流程，只是少了那句提示', async () => {
    signIn('admin')
    stubEtlMapping(null)
    renderAt(ADMIN_ROUTES.etl)

    expect(await screen.findByTestId('table-import-flow')).toBeTruthy()
    expect(screen.queryByText(/已经配好了一份映射/)).toBeNull()
  })

  it('映射状态未知时，不抢先渲染任何一种形态', async () => {
    // 抢先渲染会让刚走完引导的用户先看到"没有映射"的样子，然后闪一下变掉。
    // 未知就是未知，不许折叠进任何一个已知态。
    signIn('admin')
    renderAt(ADMIN_ROUTES.etl)

    expect(await screen.findByTestId('etl-mapping-loading')).toBeTruthy()
    expect(screen.queryByTestId('table-import-flow')).toBeNull()
    expect(screen.queryByText(/已经配好了一份映射/)).toBeNull()
  })
})

describe('预演转正式执行', () => {
  it('预演报告页能直接正式执行，不用重传文件', async () => {
    signIn('admin')
    stubSelectedRun({ dry_run: true, status: 'completed' })
    renderAt(ADMIN_ROUTES.etl)
    const user = userEvent.setup()
    // 选中这条跑批记录，才会渲染详情区——按钮挂在详情区里。
    await user.click(await screen.findByText('run-1'))
    await user.click(await screen.findByRole('button', { name: '按这次预演正式执行' }))
    expect(lastRequest()?.url).toMatch(/\/promote$/)
  })

  it('按钮旁说清转正会重新受安全阀限制，不承诺「按这次预演」包括那个开关', async () => {
    // 转正硬编码 allow_large_sweep=false（一次点击不该能触发大规模清理）。
    // 勾了那个开关跑出来的预演转正时会直接撞阀失败，用户只能回去重传两个
    // 文件——正是这个功能要消除的那件事，且是在最需要它的场景里回来的。
    // 本轮不改这个行为，但按钮不能对此只字不提。
    signIn('admin')
    stubSelectedRun({ dry_run: true, status: 'completed' })
    renderAt(ADMIN_ROUTES.etl)
    const user = userEvent.setup()
    await user.click(await screen.findByText('run-1'))
    await screen.findByRole('button', { name: '按这次预演正式执行' })
    const notice = screen.getByText(/正式执行会重新受大规模清理安全阀限制/)

    // 撞阀时到底发生了什么，两道阀的答案不一样：实体侧那道在任何写入之前
    // 触发（schema_etl.py:436），整轮零改动；关系侧那道弱一档
    // （schema_etl.py:536），触发时新边已写、陈旧边未删。文案曾经写成
    // 「什么都不改地失败」，对关系侧是假的——一句用户可见的、关于数据
    // 安全的假话。下面两条断言钉的就是这个区分：删除没发生（真），但
    // 「什么都没改」不许再出现。
    expect(notice.textContent).toMatch(/不会删除任何东西/)
    expect(notice.textContent).toMatch(/关系侧/)
    expect(notice.textContent).not.toMatch(/什么都不改/)
  })

  it('正式运行的报告页没有这个按钮', async () => {
    signIn('admin')
    stubSelectedRun({ dry_run: false, status: 'completed' })
    renderAt(ADMIN_ROUTES.etl)
    const user = userEvent.setup()
    // 跟第一条用例一样先选中这条跑批——不选中的话详情区（按钮所在的地方）
    // 根本不会挂载，断言会在"实现对不对都通过"的假位置上，等于没测。
    await user.click(await screen.findByText('run-1'))
    await screen.findByText(/已完成/)
    expect(screen.queryByRole('button', { name: '按这次预演正式执行' })).toBeNull()
  })
})
