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
import type { EtlMapping } from './etlMappingApi'
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

function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/auth/whoami')) return whoamiResponse()
      requests.push({ url, init })
      const json = (body: unknown, status = 200) =>
        Promise.resolve(new Response(JSON.stringify(body), { status }))
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
        return json({ run_id: startRunOutcome?.listRow.run_id ?? 'run-new' })
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

describe('已有映射时只传数据文件就能跑', () => {
  it('主区域就有数据文件输入框，不用先点开「高级」', async () => {
    // 页面明说"传入数据文件即可运行，不用再配一遍"。而在这之前，唯一能传
    // 数据文件的地方折叠在一个叫「高级」、副标题是"已经有验证过的配置文件？"
    // 的面板里，那个表单还要求再传一次 config.yaml——正是那句话承诺不用做的事。
    signIn('admin')
    stubEtlMapping({
      config_yaml: 'entities: []',
      source_file_name: 'soft_drink_sales.xlsx',
      created_at: '2026-09-11T00:00:00',
    })
    renderAt(ADMIN_ROUTES.etl)
    // 不做任何展开动作就该看得见。
    expect(await screen.findByTestId('run-with-stored-mapping')).toBeTruthy()
  })

  it('运行表单里先摆出映射摘要：哪列是身份键、哪列挂在哪个实体下', async () => {
    // 真实事故：邮编挂在「客户名」下，同名客户邮编不同，跑批被拒。表单此前只
    // 说"传数据文件即可运行"，用户没看过映射就点了。摘要要把这件事直接摆出来。
    signIn('admin')
    stubEtlMapping({
      config_yaml: 'entities: []',
      source_file_name: 'soft_drink_sales.xlsx',
      created_at: '2026-09-11T00:00:00',
      summary: {
        entities: [
          {
            term_type: 'Customer Name',
            source_file: 'soft_drink_sales.xlsx',
            key_columns: ['Customer Name'],
            key_parts: [{ kind: 'column' as const, column: 'Customer Name' }],
            name_columns: ['Customer Name'],
            attributes: { Customer_Zip_Code: 'Customer Zip Code' },
          },
          {
            term_type: 'Order ID',
            source_file: 'soft_drink_sales.xlsx',
            key_columns: ['Order ID'],
            key_parts: [{ kind: 'column' as const, column: 'Order ID' }],
            name_columns: ['Order ID'],
            attributes: {},
          },
        ],
        relations: [
          { relation_type: 'placed_by', subject_term_type: 'Order ID', object_term_type: 'Customer Name' },
        ],
      },
    })
    renderAt(ADMIN_ROUTES.etl)
    const form = await screen.findByTestId('run-with-stored-mapping')
    const summary = within(form).getByTestId('mapping-summary')
    // 邮编那一行必须跟「Customer Name」挂在同一个条目里，不是随便出现在页面上。
    const customerRow = within(summary).getByText('Customer Name', { selector: '.font-bold' }).closest('li')!
    expect(within(customerRow).getByText('Customer Zip Code')).toBeTruthy()
    expect(within(summary).getByText(/Order ID —placed_by→ Customer Name/)).toBeTruthy()
  })

  it('存好的 YAML 解析不出摘要时，表单照常可用、不画摘要', async () => {
    signIn('admin')
    stubEtlMapping({
      config_yaml: 'entities: []',
      source_file_name: 'soft_drink_sales.xlsx',
      created_at: '2026-09-11T00:00:00',
      summary: null,
    })
    renderAt(ADMIN_ROUTES.etl)
    const form = await screen.findByTestId('run-with-stored-mapping')
    expect(within(form).queryByTestId('mapping-summary')).toBeNull()
    expect(within(form).getByLabelText(/数据文件/)).toBeTruthy()
  })

  it('提交时不带 config，让后端用存好的那份', async () => {
    // 带上一个空 config 的话后端会拿它当权威，跑出一份什么都不导的空跑批。
    signIn('admin')
    stubEtlMapping({
      config_yaml: 'entities: []',
      source_file_name: 'soft_drink_sales.xlsx',
      created_at: '2026-09-11T00:00:00',
    })
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.etl)
    const form = await screen.findByTestId('run-with-stored-mapping')
    // 等按钮真的可用：确认状态没拿到之前整个表单是禁用的（本体没确认就不该
    // 能触发 ETL）。不等的话点击落在一个 disabled 的按钮上，什么都不会发生。
    const button = within(form).getByRole('button', { name: /开始运行|运行/ })
    await waitFor(() => expect(button.hasAttribute('disabled')).toBe(false))
    const input = within(form).getByLabelText(/数据文件/) as HTMLInputElement
    await user.upload(input, new File(['a,b,1,2'], 'sales_2026.csv', { type: 'text/csv' }))
    await user.click(button)

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

  it('提交后跑批失败，原因不用点任何东西就能看见', async () => {
    // 真实事故：demo 租户导入 soft_drink_sales.xlsx，ETL 因为 530 个客户
    // 同名不同邮编拒绝写入——原因完整地存在 etl_runs.error 里，后端日志
    // 也打了。但页面提交后只刷新列表、不选中新跑批，用户看到的是一行
    // 「失败」徽标；原因藏在要点那一行才展开的详情里。他以为是"上传坏了"。
    signIn('admin')
    stubEtlMapping({
      config_yaml: 'entities: []',
      source_file_name: 'soft_drink_sales.xlsx',
      created_at: '2026-09-11T00:00:00',
    })
    const reason =
      "实体类型 'Customer Name' 有 530 个 node_key 被算出了不同的值，本次未写入任何数据。"
    startRunOutcome = {
      listRow: {
        run_id: 'run-failed',
        status: 'failed',
        started_at: '2026-09-14T09:58:59',
        finished_at: '2026-09-14T09:59:04',
      },
      detail: {
        run_id: 'run-failed',
        status: 'failed',
        started_at: '2026-09-14T09:58:59',
        finished_at: '2026-09-14T09:59:04',
        error: reason,
        report: null,
      },
    }
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.etl)
    const form = await screen.findByTestId('run-with-stored-mapping')
    const button = within(form).getByRole('button', { name: /开始运行|运行/ })
    await waitFor(() => expect(button.hasAttribute('disabled')).toBe(false))
    await user.upload(
      within(form).getByLabelText(/数据文件/) as HTMLInputElement,
      new File(['a,b'], 'soft_drink_sales.xlsx'),
    )
    await user.click(button)

    // 提交那一刻它还在跑。详情已经开着，但没有失败原因可显示。
    await screen.findByText(/跑批详情：run-failed/)
    expect(screen.queryByRole('alert')).toBeNull()

    // ETL 跑完了、失败了。页面自己轮询列表（running 时每 3 秒），详情得跟着
    // 刷新——不点任何一行，原因就得在页面上出现。
    settleRun!()
    const alert = await screen.findByRole('alert', {}, { timeout: 8000 })
    expect(alert.textContent).toContain('Customer Name')
    expect(alert.textContent).toContain('530')

    // 原因十有八九在映射上，不在文件上。只报原因不给出路，用户会去重新上传
    // 同一个文件再失败一次。点「去改映射」要把构建器展开。
    await user.click(screen.getByTestId('fix-mapping-from-failure'))
    expect(await screen.findByText(/表格列 ↔ 本体实体/)).toBeTruthy()
    // 展开状态从三角形的朝向读不出来，从"构建器内容挂没挂载"读：它自己
    // 的第二步标题就是「配置实体映射」——哪列是身份键、属性挂在哪个实体下。
    expect(await screen.findByText(/2\. 配置实体映射/)).toBeTruthy()
  }, 15000)

  it('一个文件都没选就点运行，给出提示而不是发一次空请求', async () => {
    // 这里不能靠 <input required>：jsdom 的表单校验不认 user-event 设进去的
    // files，整个提交会被静默挡掉，于是"能提交"这件事在测试里根本验不了。
    // 守卫写在 handleUpload 里，这条用例钉住它。
    signIn('admin')
    stubEtlMapping({
      config_yaml: 'entities: []',
      source_file_name: 'soft_drink_sales.xlsx',
      created_at: '2026-09-11T00:00:00',
    })
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.etl)
    const form = await screen.findByTestId('run-with-stored-mapping')
    const button = within(form).getByRole('button', { name: /开始运行|运行/ })
    await waitFor(() => expect(button.hasAttribute('disabled')).toBe(false))
    await user.click(button)

    expect(await within(form).findByRole('alert')).toBeTruthy()
    expect(
      requests.find((r) => r.url.includes('/schema-etl/runs') && r.init?.method === 'POST'),
    ).toBeUndefined()
  })

  it('没有映射时不出这个表单——没东西可回落', async () => {
    signIn('admin')
    stubEtlMapping(null)
    renderAt(ADMIN_ROUTES.etl)
    await screen.findByTestId('admin-topbar')
    expect(screen.queryByTestId('run-with-stored-mapping')).toBeNull()
  })
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
  it('本体带着引导配好的映射时，只要数据文件，不邀请他重配', async () => {
    // 刚走完引导的用户看到一个邀请他从头配置的界面，等于让他重做刚做完的
    // 工作；重做出来的两份还可能不一致，那时以哪个为准没有答案。
    signIn('admin')
    stubEtlMapping({
      config_yaml: 'entities: []',
      source_file_name: 'orders.csv',
      created_at: '2026-09-03T00:00:00',
    })
    renderAt(ADMIN_ROUTES.etl)
    expect(await screen.findByText(/引导流程已为这个本体配好映射/)).toBeTruthy()
    expect(screen.getByText('orders.csv')).toBeTruthy()
    // 构建器降级成折叠的次级入口，不是主角。
    expect(screen.getByRole('button', { name: /表格列 ↔ 本体实体的映射/ })).toBeTruthy()
    // 光断言按钮存在区分不了折叠和展开——两种状态下按钮都在。真正能区分
    // 开的是面板内容："1. 添加数据文件" 是 SchemaEtlConfigBuilder 展开后
    // 才会渲染的第一行，折叠时整个组件都不挂载，这行文本不存在。
    expect(screen.queryByText('1. 添加数据文件')).toBeNull()
  })

  it('没有映射时维持原样，构建器是主角', async () => {
    signIn('admin')
    stubEtlMapping(null)
    renderAt(ADMIN_ROUTES.etl)
    expect(await screen.findByRole('button', { name: /把这张表的列映射到本体实体/ })).toBeTruthy()
    expect(screen.queryByText(/引导流程已为这个本体配好映射/)).toBeNull()
    // 无映射时构建器默认展开，不需要用户先点开折叠按钮才看到内容。
    expect(await screen.findByText('1. 添加数据文件')).toBeTruthy()
  })

  it('映射状态未知时，不抢先渲染任何一种形态', async () => {
    // 抢先渲染"从头配置"会让刚走完引导的用户看到一个邀请他重做的界面，
    // 然后闪一下变掉。未知就是未知，不许折叠进任何一个已知态。
    signIn('admin')
    renderAt(ADMIN_ROUTES.etl)
    expect(await screen.findByTestId('etl-mapping-loading')).toBeTruthy()
    expect(screen.queryByText(/引导流程已为这个本体配好映射/)).toBeNull()
    expect(screen.queryByRole('button', { name: /把这张表的列映射到本体实体/ })).toBeNull()
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
