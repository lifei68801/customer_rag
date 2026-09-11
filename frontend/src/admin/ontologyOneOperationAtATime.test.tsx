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
 * 一次只能有一个操作在飞——三个按钮说的必须是同一件事。
 *
 * 这条不变量此前散在实体类型/关系类型两个 tab 的十几个 disabled 表达式里，
 * 而且**并不一致**：
 *
 * - 「编辑」 disabled={editingValue !== null}
 * - 「删除」 disabled={deletingValue !== null || editingValue !== null}
 * - 「迁移」 disabled={migrating}
 *
 * 也就是说删除请求在飞的时候，「迁移实体类型…」照样能点开——而迁移是一个
 * 不可逆的批量操作。收进 useEditableList 之后三个按钮都看同一个 busy。
 *
 * 这条测试打在页面上而不是 hook 上：hook 的测试只能证明 run 会拒绝第二个
 * 操作，证明不了这三个按钮真的接上了它。接错一个按钮，hook 测试照样全绿。
 */

function whoamiResponse() {
  return Promise.resolve(
    new Response(
      JSON.stringify({
        username: 'alice',
        role: 'admin',
        tenant_id: 'demo',
        current_tenant_id: 'demo',
      }),
      { status: 200 },
    ),
  )
}

const TERM_TYPES = [
  { value: '产品', extra_fields: [], standard_name_value_type: 'string' },
]

/** 删除请求永远不返回——让它停在"飞行中"这个状态上供断言。 */
let releaseDelete: (() => void) | null = null

function stubApi() {
  releaseDelete = null
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
      if (url.includes('/checkout')) return json({})
      if (url.includes('/terms/summary')) return json({ groups: [] })
      if (method === 'DELETE') {
        return new Promise<Response>((resolve) => {
          releaseDelete = () => resolve(new Response('{}', { status: 200 }))
        })
      }
      if (url.includes('/term-types')) return json({ term_types: TERM_TYPES })
      if (url.includes('/relation-types')) return json({ relation_types: [] })
      if (url.includes('/constraints')) return json({ constraints: [] })
      if (url.includes('/terms')) return json({ terms: [], total: 0 })
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  resetAdminSession()
  sessionStorage.clear()
  localStorage.clear()
  stubApi()
})

async function renderOntology() {
  render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[ADMIN_ROUTES.ontology]}>
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
  await screen.findByTestId('admin-topbar')
}

describe('实体类型 tab：一次只能一个操作', () => {
  it('删除在飞的时候，编辑和迁移都点不了', async () => {
    const user = userEvent.setup()
    await renderOntology()

    const row = (await screen.findByText('产品')).closest('tr')
    expect(row).not.toBeNull()

    await user.click(within(row as HTMLElement).getByRole('button', { name: '删除' }))
    // 确认弹窗：不可逆操作都要过一道。
    // 精确名匹配：页面上还有一个「确认 schema」按钮，用正则会同时命中两个。
    await user.click(await screen.findByRole('button', { name: '确认' }))

    await waitFor(() => {
      expect(within(row as HTMLElement).getByRole('button', { name: '删除中…' })).toBeTruthy()
    })

    // 这三条是这次改动的全部意义：迁移那条在修好之前是能点的。
    expect(
      within(row as HTMLElement).getByRole('button', { name: '编辑' }),
    ).toBeDisabled()
    expect(
      within(row as HTMLElement).getByRole('button', { name: /迁移实体类型/ }),
    ).toBeDisabled()
    expect(
      within(row as HTMLElement).getByRole('button', { name: '删除中…' }),
    ).toBeDisabled()

    releaseDelete?.()
  })

  it('空闲时三个按钮都能点——闸不能把正常路径也关上', async () => {
    // 没有这一条的话，"永远 disabled"这种实现也能让上面那条通过。
    await renderOntology()

    const row = (await screen.findByText('产品')).closest('tr') as HTMLElement
    expect(within(row).getByRole('button', { name: '编辑' })).not.toBeDisabled()
    expect(within(row).getByRole('button', { name: /迁移实体类型/ })).not.toBeDisabled()
    expect(within(row).getByRole('button', { name: '删除' })).not.toBeDisabled()
  })
})
