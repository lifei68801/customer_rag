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
 * 组织管理页（spec 裁决补充：只有 admin 能进，入口在账号菜单）。
 *
 * 后端的组织 API 阶段一就有了，但一直没有页面调它——建组织、把租户挂到
 * 组织下只能走 API。存量租户因此 org_id 全空，看板"按组织分组"的能力用不上。
 */

interface Recorded {
  url: string
  method: string
  body: string
}

let requests: Recorded[] = []
let signedInRole: 'admin' | 'member' = 'admin'
let organizations: { org_id: string; name: string; status: string; tenant_ids: string[] }[]
let tenants: { tenant_id: string; name: string; status: string }[]
let createOrgError: string | null = null

function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      requests.push({ url, method, body: String(init?.body ?? '') })
      if (url.includes('/auth/whoami')) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              username: signedInRole,
              role: signedInRole,
              tenant_id: signedInRole === 'admin' ? null : 'muji-商品',
              current_tenant_id: 'muji-商品',
            }),
            { status: 200 },
          ),
        )
      }
      if (url.endsWith('/api/admin/organizations') && method === 'POST') {
        if (createOrgError) {
          return Promise.resolve(
            new Response(JSON.stringify({ detail: createOrgError }), { status: 400 }),
          )
        }
        const body = JSON.parse(String(init?.body))
        organizations = [...organizations, { ...body, status: 'active', tenant_ids: [] }]
        return Promise.resolve(new Response(JSON.stringify(body), { status: 201 }))
      }
      if (url.endsWith('/api/admin/organizations')) {
        return Promise.resolve(new Response(JSON.stringify({ organizations }), { status: 200 }))
      }
      if (url.includes('/organization') && method === 'PUT') {
        return Promise.resolve(new Response(JSON.stringify({}), { status: 200 }))
      }
      if (url.includes('/api/admin/tenants')) {
        return Promise.resolve(new Response(JSON.stringify({ tenants }), { status: 200 }))
      }
      if (url.includes('/nav-badges')) {
        return Promise.resolve(new Response(JSON.stringify({}), { status: 200 }))
      }
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  requests = []
  signedInRole = 'admin'
  createOrgError = null
  organizations = [
    { org_id: 'muji', name: '无印良品', status: 'active', tenant_ids: ['muji-商品', 'muji-门店'] },
  ]
  tenants = [
    { tenant_id: 'muji-商品', name: '导购小美', status: 'active' },
    { tenant_id: 'muji-门店', name: '店务老张', status: 'active' },
    { tenant_id: 'demo', name: '演示', status: 'active' },
  ]
  resetAdminSession()
  localStorage.clear()
  stubApi()
})

async function renderPage() {
  const result = render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[ADMIN_ROUTES.organizations]}>
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
  await screen.findByTestId('admin-topbar')
  return result
}

describe('组织管理页', () => {
  it('列出组织和各自名下的租户，没归入组织的租户也要列', async () => {
    // 没挂组织的租户不列的话，用户找不到它们，也就永远挂不上。
    await renderPage()

    const org = await screen.findByTestId('org-muji')
    expect(within(org).getByText('导购小美')).toBeTruthy()
    expect(within(org).getByText('店务老张')).toBeTruthy()

    const unassigned = screen.getByTestId('org-unassigned')
    expect(within(unassigned).getByText('演示')).toBeTruthy()
  })

  it('新建组织后列表里多一个', async () => {
    const user = userEvent.setup()
    await renderPage()
    await screen.findByTestId('org-muji')

    await user.type(screen.getByLabelText('组织 ID'), 'acme')
    await user.type(screen.getByLabelText('组织名称'), 'Acme')
    await user.click(screen.getByRole('button', { name: '新建组织' }))

    const create = await waitFor(() => {
      const found = requests.find(
        (r) => r.method === 'POST' && r.url.endsWith('/api/admin/organizations'),
      )
      expect(found).toBeTruthy()
      return found!
    })
    expect(JSON.parse(create.body)).toEqual({ org_id: 'acme', name: 'Acme' })
    await waitFor(() => expect(screen.getByTestId('org-acme')).toBeTruthy())
  })

  it('把一个租户挂到组织下发的是 PUT /tenants/{id}/organization', async () => {
    const user = userEvent.setup()
    await renderPage()
    const unassigned = await screen.findByTestId('org-unassigned')

    await user.selectOptions(within(unassigned).getByLabelText('演示 所属组织'), 'muji')

    await waitFor(() => {
      const put = requests.find((r) => r.method === 'PUT')
      expect(put).toBeTruthy()
      expect(put!.url).toContain(`/api/admin/tenants/${encodeURIComponent('demo')}/organization`)
      expect(JSON.parse(put!.body)).toEqual({ org_id: 'muji' })
    })
  })

  it('把租户移出组织发的是 org_id: null，不是空串', async () => {
    // 后端把 null 当"移出"；空串会被当成一个叫 "" 的组织，挂上去之后这个
    // 租户在任何一张组织卡里都找不到。
    const user = userEvent.setup()
    await renderPage()
    const org = await screen.findByTestId('org-muji')

    await user.selectOptions(within(org).getByLabelText('导购小美 所属组织'), '')

    await waitFor(() => {
      const put = requests.find((r) => r.method === 'PUT')
      expect(put).toBeTruthy()
      expect(JSON.parse(put!.body)).toEqual({ org_id: null })
    })
  })

  it('同 org_id 建两次时把后端那句话原样显示', async () => {
    createOrgError = "组织 'muji' 已经存在"
    const user = userEvent.setup()
    await renderPage()
    await screen.findByTestId('org-muji')

    await user.type(screen.getByLabelText('组织 ID'), 'muji')
    await user.type(screen.getByLabelText('组织名称'), '再建一次')
    await user.click(screen.getByRole('button', { name: '新建组织' }))

    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('已经存在'))
  })

  it('member 打不开这一页', async () => {
    // 账号菜单里没入口，直接输 URL 也要被挡——沿用租户管理页的做法。
    signedInRole = 'member'
    await renderPage()

    await waitFor(() => expect(screen.getByTestId('no-permission')).toBeTruthy())
    expect(requests.some((r) => r.url.endsWith('/api/admin/organizations'))).toBe(false)
  })
})
