import { beforeEach, describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
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
        return json({ ontology_confirmed: true })
      }
      if (/\/promote$/.test(url)) {
        return json({ run_id: 'promoted-run' })
      }
      if (/\/schema-etl\/runs\/[^/]+$/.test(url)) {
        return json(runDetailResponse ?? {})
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
  etlMappingStubbed = false
  etlMappingValue = null
  runsListResponse = []
  runDetailResponse = null
  requests = []
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
    expect(screen.getByRole('button', { name: /改这份映射／再接一张表/ })).toBeTruthy()
    // 光断言按钮存在区分不了折叠和展开——两种状态下按钮都在。真正能区分
    // 开的是面板内容："1. 添加数据文件" 是 SchemaEtlConfigBuilder 展开后
    // 才会渲染的第一行，折叠时整个组件都不挂载，这行文本不存在。
    expect(screen.queryByText('1. 添加数据文件')).toBeNull()
  })

  it('没有映射时维持原样，构建器是主角', async () => {
    signIn('admin')
    stubEtlMapping(null)
    renderAt(ADMIN_ROUTES.etl)
    expect(await screen.findByRole('button', { name: /把这张表映射到已有本体/ })).toBeTruthy()
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
    expect(screen.queryByRole('button', { name: /把这张表映射到已有本体/ })).toBeNull()
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
