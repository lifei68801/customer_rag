import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../../App'
import { SkinProvider } from '../SkinContext'
import { ConfirmProvider } from '../ConfirmContext'
import { ToastProvider } from '../ToastContext'
import { ADMIN_ROUTES } from '../../adminRoutes'
import { resetAdminSession } from '../useAdminAuth'

/**
 * 工作台页面的行为测试。跟 guidedPage.test.tsx 同一套搭台方式（whoami 打桩、
 * 整个 App 挂在 MemoryRouter 里走真实路由），因为要验证的正是"这条路由现在
 * 渲染的是工作台"。
 */
let signedInRole: 'admin' | 'member' | null = null
let workspace: unknown = null
const saved: unknown[] = []
// 只给"点两下按钮之间要不要禁用"这类测试用：PUT 的响应故意拖一拖，好在
// 它还没回来的那个窗口里断言按钮状态。
let putDelayMs = 0

const SKILLS = [
  {
    name: 'consumer_retail',
    version: '1',
    display_name: '消费品零售',
    description: '面向品牌方/零售商的骨架',
    term_types: [
      {
        value: 'SKU',
        display_name: '商品',
        standard_name_value_type: 'string',
        extra_fields: [{ name: 'color', value_type: 'string', display_name: '颜色' }],
        key_aliases: ['jan'],
        field_aliases: { color: ['现地语色'] },
      },
    ],
    relation_types: [{ relation_type: 'SOLD_AT', example_phrase: '', description: '' }],
    constraints: [],
    questions: [],
    match_hint: '',
  },
]

function workspaceWith(termReview: 'pending' | 'accepted') {
  return {
    tenant_id: 'demo',
    skill_name: 'consumer_retail',
    skill_version: '1',
    updated_at: '2026-09-16T10:00:00',
    updated_by: 'alice',
    state: {
      term_types: [
        {
          value: 'SKU',
          display_name: '商品',
          provenance: 'skill',
          review: termReview,
          standard_name_value_type: 'string',
          extra_fields: [],
          key_aliases: ['jan'],
          field_aliases: {},
          clues: [],
          data_match: null,
        },
      ],
      relation_types: [
        {
          relation_type: 'SOLD_AT',
          example_phrase: '某商品在某门店有售',
          description: '',
          provenance: 'skill',
          review: 'accepted',
          clues: [],
          data_match: null,
        },
      ],
      // 约束没有审阅入口，永远是 pending；它进不进草稿看引用的三个元素
      constraints: [
        { subject: 'SKU', relation: 'SOLD_AT', object: 'SKU', provenance: 'skill', review: 'pending' },
      ],
      sources: [],
      unmatched_columns: {},
      questions: [],
    },
  }
}

const json = (body: unknown, status = 200) =>
  Promise.resolve(new Response(JSON.stringify(body), { status }))

function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      if (url.includes('/auth/whoami')) {
        if (signedInRole === null) return json({ detail: '未登录' }, 401)
        return json({
          username: 'alice',
          role: signedInRole,
          tenant_id: signedInRole === 'admin' ? null : 'demo',
          current_tenant_id: 'demo',
        })
      }
      if (url.includes('/nav-badges')) {
        return json({ pending_relations: 0, pending_duplicates: 0, total_terms: 0 })
      }
      if (url.includes('/modeling-workspace/skills')) return json({ skills: SKILLS })
      if (url.includes('/modeling-workspace/grounding')) {
        return json({
          status: null,
          grounded_term_types: [],
          grounded_relation_types: [],
          source_files: [],
          parse_error: null,
        })
      }
      if (url.includes('/modeling-workspace/apply-preview')) {
        return json({
          added_term_types: ['SKU'],
          removed_term_types: ['手工加的'],
          changed_term_types: [],
          added_relation_types: [],
          removed_relation_types: [],
          added_constraints: [],
          removed_constraints: [],
        })
      }
      if (url.includes('/modeling-workspace')) {
        if (method === 'POST') {
          workspace = workspaceWith('pending')
          return json({ workspace })
        }
        if (method === 'PUT') {
          const body = JSON.parse(String(init?.body))
          saved.push(body)
          workspace = { ...workspaceWith('pending'), state: body.state, updated_at: 'later' }
          if (putDelayMs > 0) {
            return new Promise((resolve) =>
              setTimeout(() => resolve(new Response(JSON.stringify({ workspace }), { status: 200 })), putDelayMs),
            )
          }
          return json({ workspace })
        }
        return json({ workspace })
      }
      if (url.includes('/draft/replace')) return json({ replaced: true })
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  signedInRole = null
  workspace = null
  saved.length = 0
  putDelayMs = 0
  resetAdminSession()
  sessionStorage.clear()
  localStorage.clear()
  stubApi()
})

function renderWorkbench() {
  return render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[ADMIN_ROUTES.guidedOntology]}>
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
}

describe('建模工作台', () => {
  it('没有工作区时列出内置领域模板', async () => {
    signedInRole = 'member'
    renderWorkbench()
    expect(await screen.findByText('消费品零售')).toBeInTheDocument()
    expect(screen.getByText(/1 个实体类型/)).toBeInTheDocument()
    // 下一步建议：第一次来的人要知道从哪开始
    expect(screen.getByText(/先选一个领域模板起步/)).toBeInTheDocument()
  })

  it('member 也能用——工作台不是管理员专属', async () => {
    signedInRole = 'member'
    renderWorkbench()
    expect(await screen.findByText('消费品零售')).toBeInTheDocument()
    expect(screen.queryByText(/只有管理员/)).not.toBeInTheDocument()
  })

  it('选模板起步后进入骨架面板，元素标着来源和待审', async () => {
    signedInRole = 'member'
    renderWorkbench()
    await userEvent.click(await screen.findByRole('button', { name: /用这个模板起步/ }))
    expect(await screen.findByText('SKU')).toBeInTheDocument()
    // 夹具里实体和关系各一条都来自模板，两条都要标出来源
    expect(screen.getAllByText('来自模板')).toHaveLength(2)
    expect(screen.getByText(/审阅骨架/)).toBeInTheDocument()
  })

  it('接受一个实体类型会把整份工作区存回去', async () => {
    signedInRole = 'member'
    workspace = workspaceWith('pending')
    renderWorkbench()
    await userEvent.click(await screen.findByRole('button', { name: '接受 SKU' }))
    await waitFor(() => expect(saved).toHaveLength(1))
    const body = saved[0] as { state: { term_types: { review: string }[] }; updated_at: string }
    expect(body.state.term_types[0].review).toBe('accepted')
    // 乐观锁：带上手上这份的时间戳
    expect(body.updated_at).toBe('2026-09-16T10:00:00')
  })

  it('拒绝的元素留在界面上，不消失', async () => {
    signedInRole = 'member'
    workspace = workspaceWith('pending')
    renderWorkbench()
    await userEvent.click(await screen.findByRole('button', { name: '拒绝 SKU' }))
    // 等状态真的落地（"已拒绝"标签出现）再断言，而不是 saved.length 一到 1
    // 就看——mock 是同步 push 的，那时 setWorkspace 可能还没把新状态渲染出来，
    // 看到的会是保存前的 DOM，不能证明"拒绝之后还在"。
    expect(await screen.findByText('已拒绝')).toBeInTheDocument()
    expect(screen.getByText('SKU')).toBeInTheDocument()
  })

  it('未落地清单把没有数据支撑的元素聚到一起', async () => {
    signedInRole = 'member'
    workspace = workspaceWith('accepted')
    renderWorkbench()
    await userEvent.click(await screen.findByRole('button', { name: /未落地/ }))
    expect(await screen.findByText(/SKU/)).toBeInTheDocument()
    expect(screen.getByText(/还没有数据支撑/)).toBeInTheDocument()
  })

  it('应用面板先显示差异，删除项单独醒目列出', async () => {
    signedInRole = 'member'
    workspace = workspaceWith('accepted')
    renderWorkbench()
    await userEvent.click(await screen.findByRole('button', { name: /^应用$/ }))
    await userEvent.click(await screen.findByRole('button', { name: /看看会改什么/ }))
    expect(await screen.findByText(/会新增：SKU/)).toBeInTheDocument()
    expect(screen.getByText(/会删掉：手工加的/)).toBeInTheDocument()
    // 没看过 diff 之前不给写入，避免静默删掉用户在本体结构页手工加的东西
    expect(screen.getByRole('button', { name: /写入草稿/ })).toBeEnabled()
  })

  it('骨架面板的改动会让已经算好的差异过期', async () => {
    signedInRole = 'member'
    workspace = workspaceWith('accepted')
    renderWorkbench()
    await userEvent.click(await screen.findByRole('button', { name: /^应用$/ }))
    await userEvent.click(await screen.findByRole('button', { name: /看看会改什么/ }))
    expect(await screen.findByText(/会新增：SKU/)).toBeInTheDocument()
    // 回骨架面板拒绝一条——这一步会存回后端，跟应用面板算出的那份差异
    // 已经对不上了。
    await userEvent.click(await screen.findByRole('button', { name: /^骨架$/ }))
    await userEvent.click(await screen.findByRole('button', { name: '拒绝 SKU' }))
    await waitFor(() => expect(saved).toHaveLength(1))
    await userEvent.click(await screen.findByRole('button', { name: /^应用$/ }))
    // 差异区域回到"还没算过"的提示，而不是继续显示那份已经过期的差异
    expect(await screen.findByText(/先点「看看会改什么」/)).toBeInTheDocument()
  })

  it('写入草稿走既有的 draft/replace', async () => {
    signedInRole = 'member'
    workspace = workspaceWith('accepted')
    renderWorkbench()
    await userEvent.click(await screen.findByRole('button', { name: /^应用$/ }))
    await userEvent.click(await screen.findByRole('button', { name: /看看会改什么/ }))
    await userEvent.click(await screen.findByRole('button', { name: /写入草稿/ }))
    // diff 里有删除项：整份替换会把用户在「本体结构」页手工加的东西删掉，
    // 这是本项目唯一"点一下就永久删别处数据"的动作，弹一次确认框拦一下。
    await userEvent.click(await screen.findByRole('button', { name: '继续写入' }))
    await waitFor(() => {
      const calls = (fetch as unknown as { mock: { calls: [string, RequestInit?][] } }).mock.calls
      expect(calls.some(([url]) => String(url).includes('/draft/replace'))).toBe(true)
    })
  })

  it('接受 SKU 后写入草稿，pending 的约束随它引用的元素一起进 payload', async () => {
    signedInRole = 'member'
    workspace = workspaceWith('pending')
    renderWorkbench()
    await userEvent.click(await screen.findByRole('button', { name: '接受 SKU' }))
    await waitFor(() => expect(saved).toHaveLength(1))
    await userEvent.click(await screen.findByRole('button', { name: /^应用$/ }))
    await userEvent.click(await screen.findByRole('button', { name: /写入草稿/ }))
    await userEvent.click(await screen.findByRole('button', { name: '继续写入' }))
    await waitFor(() => {
      const calls = (fetch as unknown as { mock: { calls: [string, RequestInit?][] } }).mock.calls
      const replace = calls.find(([url]) => String(url).includes('/draft/replace'))
      expect(replace).toBeDefined()
      const body = JSON.parse(String(replace![1]?.body)) as {
        constraints: { subject_term_type: string; relation_type: string; object_term_type: string }[]
      }
      expect(body.constraints).toEqual([
        { subject_term_type: 'SKU', relation_type: 'SOLD_AT', object_term_type: 'SKU' },
      ])
    })
  })

  it('在确认框里点取消，不会写入草稿', async () => {
    signedInRole = 'member'
    workspace = workspaceWith('accepted')
    renderWorkbench()
    await userEvent.click(await screen.findByRole('button', { name: /^应用$/ }))
    await userEvent.click(await screen.findByRole('button', { name: /看看会改什么/ }))
    await userEvent.click(await screen.findByRole('button', { name: /写入草稿/ }))
    await userEvent.click(await screen.findByRole('button', { name: '取消' }))
    // 取消之后要给操作留出反应时间，再确认请求确实没有发出去——这条测试
    // 存在的意义就是证明这层确认框真的拦得住写入，不是摆设。
    await new Promise((resolve) => setTimeout(resolve, 50))
    const calls = (fetch as unknown as { mock: { calls: [string, RequestInit?][] } }).mock.calls
    expect(calls.some(([url]) => String(url).includes('/draft/replace'))).toBe(false)
  })

  it('保存还没落地之前，骨架面板的按钮全部禁用——避免连点两下撞乐观锁', async () => {
    signedInRole = 'member'
    workspace = workspaceWith('pending')
    putDelayMs = 50
    renderWorkbench()
    await userEvent.click(await screen.findByRole('button', { name: '接受 SKU' }))
    // PUT 还没回来：这时点「拒绝 SKU」会带着同一份旧 updated_at 发第二个
    // PUT，后端必然 409。按钮此刻必须是禁用的。
    expect(await screen.findByRole('button', { name: '拒绝 SKU' })).toBeDisabled()
    await waitFor(() => expect(saved).toHaveLength(1))
  })

  it('跳过「看看会改什么」直接点「写入草稿」也会先算差异、弹确认框', async () => {
    signedInRole = 'member'
    workspace = workspaceWith('accepted')
    renderWorkbench()
    await userEvent.click(await screen.findByRole('button', { name: /^应用$/ }))
    // 故意不点「看看会改什么」：diff 这时还是 null，红框也没出现过。
    await userEvent.click(await screen.findByRole('button', { name: /写入草稿/ }))
    // 确认框必须出现——不能因为没点过预览，删除项就悄悄绕过这层拦截。
    const confirmButton = await screen.findByRole('button', { name: '继续写入' })
    const calls = (fetch as unknown as { mock: { calls: [string, RequestInit?][] } }).mock.calls
    expect(calls.some(([url]) => String(url).includes('/draft/replace'))).toBe(false)
    await userEvent.click(confirmButton)
    await waitFor(() => {
      expect(calls.some(([url]) => String(url).includes('/draft/replace'))).toBe(true)
    })
  })

  it('改名会连同约束里的引用一起存回去', async () => {
    signedInRole = 'member'
    workspace = workspaceWith('pending')
    renderWorkbench()
    const input = await screen.findByLabelText('SKU 的新名字')
    await userEvent.clear(input)
    await userEvent.type(input, '商品')
    await userEvent.click(screen.getByRole('button', { name: '改名 SKU' }))
    await waitFor(() => expect(saved).toHaveLength(1))
    const body = saved[0] as { state: { term_types: { value: string }[] } }
    expect(body.state.term_types[0].value).toBe('商品')
  })

  it('能给未落地的元素加一条人工旁证', async () => {
    signedInRole = 'member'
    workspace = workspaceWith('accepted')
    renderWorkbench()
    await userEvent.type(await screen.findByLabelText('给 SKU 加旁证'), '数据下个月接')
    await userEvent.click(screen.getByRole('button', { name: '加旁证 SKU' }))
    await waitFor(() => expect(saved).toHaveLength(1))
    const body = saved[0] as { state: { term_types: { clues: { note: string }[] }[] } }
    expect(body.state.term_types[0].clues[0].note).toBe('数据下个月接')
  })

  it('能手工新增一个实体类型', async () => {
    signedInRole = 'member'
    workspace = workspaceWith('accepted')
    renderWorkbench()
    await userEvent.type(await screen.findByLabelText('新实体类型名'), '促销活动')
    await userEvent.click(screen.getByRole('button', { name: '新增实体类型' }))
    await waitFor(() => expect(saved).toHaveLength(1))
    const body = saved[0] as { state: { term_types: { value: string; provenance: string }[] } }
    expect(body.state.term_types.some((t) => t.value === '促销活动' && t.provenance === 'manual')).toBe(
      true,
    )
  })
})
