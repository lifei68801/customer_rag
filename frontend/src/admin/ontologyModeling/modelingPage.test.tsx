import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import App from '../../App'
import { SkinProvider } from '../SkinContext'
import { ConfirmProvider } from '../ConfirmContext'
import { ToastProvider } from '../ToastContext'
import { ADMIN_ROUTES, modelingWay } from '../../adminRoutes'
import { resetAdminSession } from '../useAdminAuth'

/**
 * 本体建模页：本体结构与建模工作台并成一页的三个 tab，顺序即流程——模板构建 /
 * 智能创建两条初始化思路在前，本体结构（终点，原「手动构建」）排最后。
 *
 * 这里测的是壳——tab 与 URL 的同步、旧地址的落点、侧边栏只剩一项、写入草稿
 * 后自动跳到本体结构并提示；三个 tab 各自的内容由它们原来的测试盖着
 * （ontologyConfirm、workbenchPage 等）。
 */

function whoami() {
  return Promise.resolve(
    new Response(
      JSON.stringify({ username: 'alice', role: 'member', tenant_id: 'demo', current_tenant_id: 'demo' }),
      { status: 200 },
    ),
  )
}

const json = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status }))

const EMPTY_DIFF = {
  added_term_types: [],
  removed_term_types: [],
  changed_term_types: [],
  added_relation_types: [],
  removed_relation_types: [],
  added_constraints: [],
  removed_constraints: [],
}

// 只给"模板构建写入草稿"那条测试用：默认 null（没有工作区），其余测试都
// 走这条缺省路径，跟改动前一样。
let templateWorkspace: unknown = null
// 同理，只给"智能创建写入草稿"那条测试用。
let smartSession: unknown = null
const draftReplaceBodies: unknown[] = []

/** 一个已经有一条 accepted 实体类型的工作区，直接进"应用"面板点写入就够。 */
function templateWorkspaceWithOneAcceptedTerm() {
  return {
    tenant_id: 'demo',
    skill_name: null,
    skill_version: null,
    updated_at: '2026-09-17T10:00:00',
    updated_by: 'alice',
    state: {
      term_types: [
        {
          value: 'SKU',
          display_name: '商品',
          provenance: 'manual',
          review: 'accepted',
          standard_name_value_type: 'string',
          extra_fields: [],
          key_aliases: [],
          field_aliases: {},
          clues: [],
          data_match: null,
        },
      ],
      relation_types: [],
      constraints: [],
      sources: [],
      unmatched_columns: {},
      questions: [],
    },
  }
}

/** 一个已经有一条 accepted 候选实体的访谈会话，直接点写入就够。 */
function smartSessionWithOneAcceptedTerm() {
  return {
    tenant_id: 'demo',
    updated_at: '2026-09-17T10:00:00',
    updated_by: 'alice',
    state: {
      turns: [
        { role: 'assistant', text: '先说说你们主要做什么生意？' },
        { role: 'user', text: '我们主要卖服装和家居' },
      ],
      skeleton: {
        term_types: [
          {
            value: '商品',
            display_name: '商品',
            rationale: '用户说主要卖服装和家居',
            confidence: 'guess',
            from_turn: 1,
            review: 'accepted',
            extra_fields: [],
            standard_name_value_type: 'string',
          },
        ],
        relation_types: [],
        constraints: [],
      },
      questions: [],
      done: false,
    },
  }
}

beforeEach(() => {
  resetAdminSession()
  sessionStorage.clear()
  localStorage.clear()
  templateWorkspace = null
  smartSession = null
  draftReplaceBodies.length = 0
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/auth/whoami')) return whoami()
      if (url.includes('/nav-badges')) return json({ pending_relations: 0, pending_duplicates: 0, total_terms: 0 })
      if (url.includes('/ontology/demo/status')) return json({ confirmed: false })
      if (url.includes('/draft/checkout')) return json({ ok: true })
      if (url.includes('/term-types')) return json({ term_types: [] })
      if (url.includes('/relation-types')) return json({ relation_types: [] })
      if (url.includes('/constraints')) return json({ constraints: [] })
      if (url.includes('/modeling-workspace/skills')) return json({ skills: [] })
      if (url.includes('/modeling-workspace/grounding'))
        return json({ status: null, grounded_term_types: [], grounded_relation_types: [], source_files: [], parse_error: null })
      // apply-preview 的 URL 里也含 '/modeling-workspace'，必须排在那条通用
      // 分支前面，否则永远走不到这里。
      if (url.includes('/modeling-workspace/apply-preview')) return json(EMPTY_DIFF)
      if (url.includes('/modeling-workspace')) return json({ workspace: templateWorkspace })
      if (url.includes('/draft/replace')) {
        draftReplaceBodies.push(JSON.parse(String(init?.body)))
        return json({ replaced: true })
      }
      if (url.includes('/interview')) return json({ session: smartSession })
      return new Promise(() => {})
    }),
  )
})

function Probe() {
  const { pathname, search } = useLocation()
  return <span data-testid="url">{pathname + search}</span>
}

function renderAt(path: string) {
  return render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[path]}>
            <Probe />
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
}

const tabs = () => within(screen.getByRole('group', { name: '构建方式' }))

describe('本体建模页', () => {
  it('侧边栏本体创建下第一项是本体建模，旧的两项没了', async () => {
    renderAt(ADMIN_ROUTES.ontologyModeling)
    const nav = within(await screen.findByRole('navigation', { name: '后台导航' }))
    const labels = nav.getAllByRole('link').map((a) => a.textContent?.trim())
    expect(labels.indexOf('本体建模')).toBeLessThan(labels.indexOf('本体图'))
    expect(labels).not.toContain('本体结构')
    expect(labels).not.toContain('建模工作台')
  })

  it('缺省落在本体结构——多数租户在维护已有本体，不该被扔进空工作区', async () => {
    renderAt(ADMIN_ROUTES.ontologyModeling)
    expect(await screen.findByRole('heading', { name: '本体建模' })).toBeInTheDocument()
    expect(tabs().getByRole('button', { name: '本体结构' }).getAttribute('aria-pressed')).toBe('true')
    // 本体结构就是原来的本体结构页：三个子 tab 还在
    expect(await screen.findByRole('button', { name: '实体类型' })).toBeInTheDocument()
  })

  it('tab 顺序与名字：两条初始化思路在前，本体结构（终点）排最后', async () => {
    // 顺序即流程（design 增补决策 13）：模板构建、智能创建只是起个头，
    // 本体结构才是所有路的终点，排最后提醒用户"这里才是要落地的地方"。
    renderAt(ADMIN_ROUTES.ontologyModeling)
    await screen.findByRole('heading', { name: '本体建模' })
    const labels = tabs()
      .getAllByRole('button')
      .map((b) => b.textContent?.trim())
    expect(labels).toEqual(['模板构建', '智能创建', '本体结构'])
  })

  it('切到模板构建改 URL 并渲染工作台', async () => {
    renderAt(ADMIN_ROUTES.ontologyModeling)
    await userEvent.click(await screen.findByRole('button', { name: '模板构建' }))
    expect(screen.getByTestId('url').textContent).toBe(modelingWay('template'))
    // 用按钮而不是文本：空白起步这四个字在这一屏出现多次（标题、说明、按钮）
    expect(await screen.findByRole('button', { name: '空白起步' })).toBeInTheDocument()
  })

  it('切 tab 保留别的查询参数（version 等）', async () => {
    renderAt(`${modelingWay('manual')}&version=confirmed`)
    await userEvent.click(await screen.findByRole('button', { name: '模板构建' }))
    expect(screen.getByTestId('url').textContent).toContain('version=confirmed')
    expect(screen.getByTestId('url').textContent).toContain('way=template')
  })

  it('版本切换器只在本体结构里', async () => {
    renderAt(modelingWay('manual'))
    expect(await screen.findByRole('group', { name: '本体版本' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '模板构建' }))
    expect(screen.queryByRole('group', { name: '本体版本' })).toBeNull()
  })

  it('旧地址重定向到对应的 tab', async () => {
    renderAt('/admin/ontology/ontology')
    await screen.findByRole('heading', { name: '本体建模' })
    expect(screen.getByTestId('url').textContent).toBe(modelingWay('manual'))
  })

  it('旧的引导建模地址重定向到模板构建', async () => {
    renderAt('/admin/ontology/guided')
    await screen.findByRole('heading', { name: '本体建模' })
    expect(screen.getByTestId('url').textContent).toBe(modelingWay('template'))
  })

  it('模板构建写入草稿成功后自动切到本体结构，并提示刚写入了什么', async () => {
    // 走完整条链路（工作区 → 应用面板 → 写入），不只测壳页的回调函数：
    // design 增补决策 11 要的是"用户点一下写入之后，界面上真的发生了什么"。
    templateWorkspace = templateWorkspaceWithOneAcceptedTerm()
    renderAt(modelingWay('template'))
    await userEvent.click(await screen.findByRole('button', { name: /^应用$/ }))
    // diff 是空的（EMPTY_DIFF）：没有删除项，点「写入草稿」不会弹确认框，
    // 一路直接写到 draft/replace。
    await userEvent.click(await screen.findByRole('button', { name: /写入草稿/ }))
    await waitFor(() => expect(draftReplaceBodies).toHaveLength(1))

    // 自动跳到本体结构：URL 里的 way 变成 manual（这个取值本身没改）。
    expect(screen.getByTestId('url').textContent).toBe(modelingWay('manual'))
    const notice = await screen.findByRole('status')
    expect(notice.textContent).toContain('刚写入')
    expect(notice.textContent).toContain('1 个实体类型')
  })

  it('智能创建写入草稿成功后同样自动切到本体结构', async () => {
    // 决策 11 说的是两个生成器都要收口。只钉住模板那条的话，智能创建这侧
    // 的 onApplied 接线断了没人会发现。
    smartSession = smartSessionWithOneAcceptedTerm()
    renderAt(modelingWay('smart'))
    await userEvent.click(await screen.findByRole('button', { name: '写入草稿' }))
    await waitFor(() => expect(draftReplaceBodies).toHaveLength(1))

    expect(screen.getByTestId('url').textContent).toBe(modelingWay('manual'))
    const notice = await screen.findByRole('status')
    expect(notice.textContent).toContain('刚写入')
  })

  it('点「知道了」提示就消失', async () => {
    templateWorkspace = templateWorkspaceWithOneAcceptedTerm()
    renderAt(modelingWay('template'))
    await userEvent.click(await screen.findByRole('button', { name: /^应用$/ }))
    await userEvent.click(await screen.findByRole('button', { name: /写入草稿/ }))
    await screen.findByRole('status')

    await userEvent.click(screen.getByRole('button', { name: '知道了' }))
    expect(screen.queryByRole('status')).toBeNull()
    // 清掉的只是提示，人还留在本体结构这一页上
    expect(screen.getByTestId('url').textContent).toBe(modelingWay('manual'))
  })

  it('手动切 tab 会清掉写入草稿留下的提示', async () => {
    templateWorkspace = templateWorkspaceWithOneAcceptedTerm()
    renderAt(modelingWay('template'))
    await userEvent.click(await screen.findByRole('button', { name: /^应用$/ }))
    await userEvent.click(await screen.findByRole('button', { name: /写入草稿/ }))
    await screen.findByRole('status')

    // 提示只对刚才那一次写入有效；用户自己点开别的 tab，就已经不是"刚才"了。
    await userEvent.click(screen.getByRole('button', { name: '模板构建' }))
    expect(screen.queryByRole('status')).toBeNull()
  })
})
