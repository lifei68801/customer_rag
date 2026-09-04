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
 * 删掉分类被"还有实体在用"挡住之后的去处。
 *
 * 后端现在会点名挡路的是哪几条术语，但用户仍然要自己去实体列表里把它们
 * 翻出来——两万条实体的租户里那是一段真实的苦工。这个文件钉住两件事：
 * 错误提示里有一条能直接筛出这些术语的链接；实体列表能读懂那个链接。
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

const IN_USE_BODY = {
  detail: "分类 'module' 仍被 1 条术语（示例登录模块）引用，无法删除",
  blocking_terms: { term_type: 'module', total: 1, node_keys: ['示例登录模块'] },
  blocking_constraints_total: 0,
}

/** 只被草稿约束挡住的 409：实体列表里没有东西可处理。 */
const CONSTRAINT_ONLY_BODY = {
  detail: "分类 'module' 仍被 1 条关系约束（module -PART_OF-> product）引用，无法删除",
  blocking_terms: { term_type: 'module', total: 0, node_keys: [] },
  blocking_constraints_total: 1,
}

let deleteBody: unknown = null

const SUMMARY = {
  groups: [
    { term_type: 'module', total: 1 },
    { term_type: '订单号', total: 10000 },
  ],
}

function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      const json = (body: unknown, status = 200) =>
        Promise.resolve(new Response(JSON.stringify(body), { status }))
      if (url.includes('/auth/whoami')) return whoamiResponse()
      if (url.includes('/nav-badges')) {
        return json({ pending_relations: 0, pending_duplicates: 0, total_terms: 1 })
      }
      if (/\/ontology\/[^/]+\/status$/.test(url)) return json({ confirmed: false })
      if (url.includes('/ontology/') && url.includes('/checkout')) return json({})
      if (url.includes('/terms/summary')) return json(SUMMARY)
      if (url.includes('/term-types/') && method === 'DELETE') {
        return json(deleteBody ?? IN_USE_BODY, 409)
      }
      if (url.includes('/term-types')) {
        return json({
          term_types: [{ value: 'module', extra_fields: [], standard_name_value_type: 'string' }],
        })
      }
      if (url.includes('/relation-types')) return json({ relation_types: [] })
      if (url.includes('/constraints')) return json({ constraints: [] })
      if (url.includes('/terms')) {
        const type = new URL(url, 'http://x').searchParams.get('term_type') ?? '未知'
        const total = SUMMARY.groups.find((g) => g.term_type === type)?.total ?? 0
        return json({
          terms: Array.from({ length: Math.min(total, 20) }, (_, i) => ({
            node_key: `${type}:${i}`,
            standard_name: `${type}-${i}`,
            aliases: [],
            term_type: type,
            extra_properties: {},
            source: 'etl',
          })),
          total,
        })
      }
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  deleteBody = null
  resetAdminSession()
  sessionStorage.clear()
  localStorage.clear()
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

describe('删除实体类型被占用时的出口', () => {
  it('错误提示里给出一条按该类型筛好的实体列表链接', async () => {
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.ontology)
    const deleteButton = await screen.findByRole('button', { name: '删除' })
    await user.click(deleteButton)
    const dialog = await screen.findByRole('alertdialog')
    await user.click(within(dialog).getByRole('button', { name: '确认' }))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('示例登录模块')
    const link = within(alert).getByRole('link')
    expect(link.getAttribute('href')).toBe(`${ADMIN_ROUTES.terms}?term_type=module`)
  })

  it('只被关系约束挡住时不给这条链接——实体列表里没东西可处理', async () => {
    deleteBody = CONSTRAINT_ONLY_BODY
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.ontology)
    await user.click(await screen.findByRole('button', { name: '删除' }))
    const dialog = await screen.findByRole('alertdialog')
    await user.click(within(dialog).getByRole('button', { name: '确认' }))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('PART_OF')
    // 链接指向一个空列表，等于把用户支去一个什么都做不了的页面。
    expect(within(alert).queryByRole('link')).toBeNull()
  })
})

describe('实体列表读 URL 上的类型过滤', () => {
  it('带 term_type 时只列出那个类型', async () => {
    renderAt(`${ADMIN_ROUTES.terms}?term_type=module`)
    await waitFor(() => expect(screen.getByRole('group', { name: /module/ })).toBeTruthy())
    // 订单号这一组必须真的不在——不带过滤时它是渲染出来的（见下一个用例），
    // 所以这条断言真能区分"过滤生效"和"参数被忽略"。
    expect(screen.queryByRole('group', { name: /订单号/ })).toBeNull()
  })

  it('不带 term_type 时列出全部类型', async () => {
    renderAt(ADMIN_ROUTES.terms)
    await waitFor(() => expect(screen.getByRole('group', { name: /订单号/ })).toBeTruthy())
    expect(screen.getByRole('group', { name: /module/ })).toBeTruthy()
  })

  it('过滤生效时说清楚这是筛过的，并给一个退出口', async () => {
    // 只显示一个类型而不说为什么，用户会以为其它实体没了——静默失败。
    const user = userEvent.setup()
    renderAt(`${ADMIN_ROUTES.terms}?term_type=module`)
    const notice = await screen.findByTestId('term-type-filter-notice')
    expect(notice.textContent).toContain('module')
    await user.click(within(notice).getByRole('button', { name: /清除|看全部/ }))
    await waitFor(() => expect(screen.getByRole('group', { name: /订单号/ })).toBeTruthy())
  })
})
