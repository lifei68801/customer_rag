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
 * 账号页「可访问的数字人」——组织与租户授权管理入口的账号侧。
 *
 * 核心风险：界面上是一组复选框，取消勾选之后保存，发出去的**必须是不含
 * 它的完整列表**，而不是"追加"或"只发变化的那个"。追加语义下取消勾选
 * 永远不生效，而界面看起来生效了——这是本项目最在意的那类静默失败，所以
 * 下面第一条用例特意从两个勾选开始、取消一个：初始只有一个的话，追加
 * 语义的实现也能让这条用例变绿。
 */

let putCalls: { username: string; tenant_ids: string[] }[] = []
let putResponse: { status: number; body: unknown } = { status: 200, body: { tenant_ids: ['b'] } }

function whoamiResponse() {
  return Promise.resolve(
    new Response(
      JSON.stringify({
        username: 'admin',
        role: 'admin',
        tenant_id: null,
        current_tenant_id: 'a',
      }),
      { status: 200 },
    ),
  )
}

function stubApi() {
  putCalls = []
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const json = (body: unknown, status = 200) =>
        Promise.resolve(new Response(JSON.stringify(body), { status }))

      if (url.includes('/auth/whoami')) return whoamiResponse()

      const tenantsGrantMatch = url.match(/\/api\/admin\/accounts\/([^/]+)\/tenants$/)
      if (tenantsGrantMatch) {
        const username = decodeURIComponent(tenantsGrantMatch[1])
        if (init?.method === 'PUT') {
          const body = JSON.parse(String(init.body)) as { tenant_ids: string[] }
          putCalls.push({ username, tenant_ids: body.tenant_ids })
          return json(putResponse.body, putResponse.status)
        }
        // GET：alice 初始被授权 a 和 b 两个租户。
        return json({ tenant_ids: ['a', 'b'] })
      }

      if (url.includes('/api/admin/accounts')) {
        return json({
          accounts: [
            {
              username: 'admin',
              role: 'admin',
              tenant_id: null,
              status: 'active',
              created_at: '2026-09-01',
              last_login_at: '2026-09-02',
            },
            {
              username: 'alice',
              role: 'member',
              tenant_id: 'a',
              status: 'active',
              created_at: '2026-09-01',
              last_login_at: null,
            },
          ],
        })
      }

      if (url.includes('/api/admin/tenants')) {
        return json({
          tenants: [
            { tenant_id: 'a', name: '租户 A', status: 'active' },
            { tenant_id: 'b', name: '租户 B', status: 'active' },
          ],
        })
      }

      if (url.includes('/nav-badges')) {
        return json({ pending_relations: 0, pending_duplicates: 0, total_terms: 0 })
      }

      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  resetAdminSession()
  sessionStorage.clear()
  localStorage.clear()
  putResponse = { status: 200, body: { tenant_ids: ['b'] } }
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

describe('账号页 · 可访问的数字人', () => {
  it('取消勾选之后保存，发出去的是不含它的完整列表', async () => {
    // 追加语义的实现会发 ['b'] 或 ['a','b']——不对，等等：追加语义在这个
    // 场景下发不出跟全量替换不同的请求体，因为"追加"通常指"只发变化"，
    // 这里验证的正是全量替换：取消 a 之后，请求体必须是 ['b']，不能带 a。
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.accounts)

    const grantsPanel = await screen.findByTestId('tenant-grants-alice')
    const checkboxA = await within(grantsPanel).findByRole('checkbox', { name: '租户 A' })
    const checkboxB = within(grantsPanel).getByRole('checkbox', { name: '租户 B' })
    await waitFor(() => expect((checkboxA as HTMLInputElement).checked).toBe(true))
    expect((checkboxB as HTMLInputElement).checked).toBe(true)

    await user.click(checkboxA)
    await user.click(
      within(grantsPanel).getByRole('button', { name: '保存 alice 可访问的数字人' }),
    )

    await waitFor(() => expect(putCalls).toHaveLength(1))
    expect(putCalls[0]).toEqual({ username: 'alice', tenant_ids: ['b'] })
  })

  it('admin 账号不显示这组复选框', async () => {
    // 后端对 admin 的 PUT 一律 400。给一个必然失败的控件，等于教用户去
    // 点一个坑，所以 admin 那一行压根不该出现这组复选框。
    renderAt(ADMIN_ROUTES.accounts)
    await screen.findByText('admin')
    expect(screen.queryByTestId('tenant-grants-admin')).toBeNull()
    // alice（member）那一行要有——反向确认这不是"整个功能没渲染"的假阴性。
    expect(await screen.findByTestId('tenant-grants-alice')).toBeTruthy()
  })

  it('保存失败时把后端那句话原样显示出来', async () => {
    // 「这些租户不存在或已停用：xxx」里有用户需要的全部信息。包装成
    // 「保存失败」等于把它扔掉。
    putResponse = { status: 400, body: { detail: '这些租户不存在或已停用：不存在' } }
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.accounts)

    const grantsPanel = await screen.findByTestId('tenant-grants-alice')
    await waitFor(() =>
      expect(
        (within(grantsPanel).getByRole('checkbox', { name: '租户 A' }) as HTMLInputElement)
          .checked,
      ).toBe(true),
    )
    await user.click(
      within(grantsPanel).getByRole('button', { name: '保存 alice 可访问的数字人' }),
    )

    expect(await within(grantsPanel).findByRole('alert')).toHaveTextContent(
      '这些租户不存在或已停用：不存在',
    )
  })
})
