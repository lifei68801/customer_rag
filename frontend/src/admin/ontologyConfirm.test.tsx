import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { ADMIN_ROUTES } from '../adminRoutes'

/**
 * 「确认 schema」这个动作在页面上的位置和出现时机。
 *
 * 它此前钉在页面头部，和一个「已确认 / 草稿中」的状态徽章并排。两个问题：
 * 徽章和侧边栏的草稿/已确认筛选说的是同一个轴的两件事，摆在一起用户会
 * 以为切换徽章能改变什么；而确认按钮在页面顶部，用户是在页面底部录完
 * 信息的——录完要回到顶部去点，中间隔着整页内容。
 *
 * 现在：徽章去掉，轴只留在侧边栏；确认按钮跟着录入信息走到页面底部，
 * 而且只在草稿视图出现——已确认视图是只读快照，那里的按钮点不动，摆着
 * 只是让人怀疑自己哪里做错了。
 */

function stubOntology({ confirmed }: { confirmed: boolean }) {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      const json = (body: unknown) =>
        Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))
      if (url.includes('/ontology/') && url.includes('/status')) return json({ confirmed })
      if (url.includes('/checkout')) return json({})
      if (url.includes('/term-types')) {
        return json({
          term_types: [{ value: '公司', extra_fields: [], standard_name_value_type: 'string' }],
        })
      }
      if (url.includes('/relation-types')) return json({ relation_types: [] })
      if (url.includes('/constraints')) return json({ constraints: [] })
      if (url.includes('/api/admin/tenants')) {
        return json({ tenants: [{ tenant_id: 'demo', name: '演示租户', status: 'active' }] })
      }
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  sessionStorage.setItem('admin_session_token', 'test-token')
  sessionStorage.setItem('admin_current_tenant', 'demo')
  localStorage.clear()
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

const confirmButton = () => screen.queryByRole('button', { name: /确认 schema/ })

describe('确认 schema 只在草稿视图出现', () => {
  it('草稿视图有这个按钮', async () => {
    stubOntology({ confirmed: false })
    renderAt(ADMIN_ROUTES.ontology)
    await waitFor(() => expect(confirmButton()).toBeTruthy())
  })

  it('已确认视图没有——那是只读快照，按钮在那儿点不动', async () => {
    stubOntology({ confirmed: true })
    renderAt(`${ADMIN_ROUTES.ontology}?version=confirmed`)
    // 等页面真的渲染出来了再断言"没有"，否则这条在加载中也会绿。
    await waitFor(() => expect(screen.getByTestId('ontology-tabs')).toBeTruthy())
    expect(confirmButton()).toBeNull()
  })

  it('排在录入区之后，不在页头', async () => {
    stubOntology({ confirmed: false })
    renderAt(ADMIN_ROUTES.ontology)
    const button = await waitFor(() => {
      const found = confirmButton()
      if (!found) throw new Error('还没渲染出来')
      return found
    })
    const panel = screen.getByTestId('ontology-tab-panel')
    // 按钮必须在 tab 内容之后。位置反了的话这一位是 PRECEDING。
    expect(panel.compareDocumentPosition(button) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
})

describe('页面里不再有第二处状态显示', () => {
  it('页头没有「已确认 / 草稿中」徽章——那个轴只留在侧边栏', async () => {
    stubOntology({ confirmed: true })
    renderAt(ADMIN_ROUTES.ontology)
    await waitFor(() => expect(screen.getByTestId('ontology-tabs')).toBeTruthy())
    expect(screen.queryByTestId('ontology-status-badge')).toBeNull()
    expect(screen.queryByText('草稿中（未确认）')).toBeNull()
  })
})

describe('切到已确认但从没确认过时，说明它为什么是空的', () => {
  it('没确认过：说清楚这份快照还不存在', async () => {
    stubOntology({ confirmed: false })
    renderAt(`${ADMIN_ROUTES.ontology}?version=confirmed`)
    // 三个空列表和「还没确认过」是两回事，不说的话用户会去查数据哪去了。
    await waitFor(() => expect(screen.getByTestId('never-confirmed-notice')).toBeTruthy())
  })

  it('确认过：不显示这句话', async () => {
    stubOntology({ confirmed: true })
    renderAt(`${ADMIN_ROUTES.ontology}?version=confirmed`)
    await waitFor(() => expect(screen.getByTestId('ontology-tabs')).toBeTruthy())
    expect(screen.queryByTestId('never-confirmed-notice')).toBeNull()
  })

  it('草稿视图不显示——那句话说的是已确认快照', async () => {
    stubOntology({ confirmed: false })
    renderAt(ADMIN_ROUTES.ontology)
    await waitFor(() => expect(screen.getByTestId('ontology-tabs')).toBeTruthy())
    expect(screen.queryByTestId('never-confirmed-notice')).toBeNull()
  })
})

describe('terms/summary 失败不阻断差异预览', () => {
  it('summary 请求失败时，确认框仍然弹出，差异预览没有退化成"无法预览"', async () => {
    const user = userEvent.setup()
    // 独立于 stubOntology：草稿和已确认版本的实体类型故意不同（产品 vs
    // 客户），这样差异预览里才有真实内容可断言，而不是空 diff 凑巧看起来
    // 也不含"无法预览"这几个字。
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input)
        const json = (body: unknown, status = 200) =>
          Promise.resolve(new Response(JSON.stringify(body), { status }))
        if (url.includes('/ontology/') && url.includes('/status')) return json({ confirmed: false })
        if (url.includes('/checkout')) return json({})
        if (url.includes('/term-types')) {
          return json({
            term_types: url.includes('status=confirmed')
              ? [{ value: '客户', extra_fields: [], standard_name_value_type: 'string' }]
              : [{ value: '产品', extra_fields: [], standard_name_value_type: 'string' }],
          })
        }
        if (url.includes('/relation-types')) {
          return json({
            relation_types: [
              { relation_type: 'HAS', example_phrase: 'x has y', description: '', allow_chain_query: false },
            ],
          })
        }
        if (url.includes('/constraints')) {
          return json({
            constraints: [{ subject_term_type: '产品', relation_type: 'HAS', object_term_type: '产品' }],
          })
        }
        if (url.includes('/terms/summary')) {
          // 非 2xx：fetchTermsSummary 里 `if (!response.ok) throw new
          // Error(extractErrorDetail(...))` 命中，返回的 promise 会
          // reject——这条走的是 handleConfirm 里 `.then(success, failure)`
          // 的 failure 分支，不是随便哪种"失败"都巧合地得到同样结果。
          return json({ detail: '统计服务暂不可用' }, 500)
        }
        if (url.includes('/api/admin/tenants')) {
          return json({ tenants: [{ tenant_id: 'demo', name: '演示租户', status: 'active' }] })
        }
        return new Promise(() => {})
      }),
    )
    renderAt(ADMIN_ROUTES.ontology)
    const button = await screen.findByRole('button', { name: /确认 schema/ })
    await waitFor(() => expect(button).not.toBeDisabled())
    await user.click(button)
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog.textContent).not.toMatch(/无法预览本次变更/)
    expect(dialog.textContent).toMatch(/产品/)
    expect(dialog.textContent).toMatch(/客户/)
  })
})
describe('terms/summary 挂起时按钮要有反应', () => {
  it('summary 请求一直不返回时，按钮立刻变成"确认中…"并禁用', async () => {
    // 挂起不是失败：pending 的 promise 既不 resolve 也不 reject，
    // fetchTermsSummary 那个 .then(ok, err) 的 err 分支和外层 catch 都
    // 兜不住它，Promise.all 永不落地、confirm() 永不被调用。修之前的表现
    // 是：用户点了确认，界面完全没反应（按钮不变灰、不改字、不弹框），
    // 他会再点一次，每点一次多发三路请求。
    const user = userEvent.setup()
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input)
        const json = (body: unknown) =>
          Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))
        if (url.includes('/ontology/') && url.includes('/status')) return json({ confirmed: false })
        if (url.includes('/checkout')) return json({})
        // 这一路永不落地——被测的就是它。放在 /term-types 之前匹配，
        // 免得将来两条规则的先后顺序变了又悄悄测不到。
        if (url.includes('/terms/summary')) return new Promise(() => {})
        if (url.includes('/term-types')) {
          return json({
            term_types: [{ value: '公司', extra_fields: [], standard_name_value_type: 'string' }],
          })
        }
        // 关系类型和约束都得非空，确认按钮才是可点的（缺任何一样都会被
        // confirmDisabledReason 拦下，那样这条用例就测不到点击之后的事）。
        if (url.includes('/relation-types')) {
          return json({
            relation_types: [
              { relation_type: 'HAS', example_phrase: 'x has y', description: '', allow_chain_query: false },
            ],
          })
        }
        if (url.includes('/constraints')) {
          return json({
            constraints: [{ subject_term_type: '公司', relation_type: 'HAS', object_term_type: '公司' }],
          })
        }
        if (url.includes('/api/admin/tenants')) {
          return json({ tenants: [{ tenant_id: 'demo', name: '演示租户', status: 'active' }] })
        }
        return new Promise(() => {})
      }),
    )
    renderAt(ADMIN_ROUTES.ontology)
    const button = await screen.findByRole('button', { name: /确认 schema/ })
    await waitFor(() => expect(button).not.toBeDisabled())
    await user.click(button)
    await waitFor(() => expect(button.textContent).toMatch(/确认中/))
    expect(button).toBeDisabled()
    // 确认框还没弹出来——按钮的这个状态说的正是"正在算差异"这个空档。
    expect(screen.queryByRole('alertdialog')).toBeNull()
  })
})
