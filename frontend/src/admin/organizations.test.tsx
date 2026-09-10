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
let deleteStatus = 200
let deleteDetail = ''
let assignStatus = 200
let assignDetail = ''

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
        if (assignStatus !== 200) {
          return Promise.resolve(
            new Response(JSON.stringify({ detail: assignDetail }), { status: assignStatus }),
          )
        }
        return Promise.resolve(new Response(JSON.stringify({}), { status: 200 }))
      }
      if (/\/organizations\/[^/]+\/(disable|enable)$/.test(url) && method === 'POST') {
        const disabling = url.endsWith('/disable')
        organizations = organizations.map((o) =>
          url.includes(`/${o.org_id}/`) ? { ...o, status: disabling ? 'disabled' : 'active' } : o,
        )
        return Promise.resolve(new Response(JSON.stringify({}), { status: 200 }))
      }
      if (/\/organizations\/[^/]+$/.test(url) && method === 'DELETE') {
        if (deleteStatus !== 200) {
          return Promise.resolve(
            new Response(JSON.stringify({ detail: deleteDetail }), { status: deleteStatus }),
          )
        }
        organizations = organizations.filter((o) => !url.endsWith(`/${o.org_id}`))
        return Promise.resolve(new Response(JSON.stringify({ deleted: true }), { status: 200 }))
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
  deleteStatus = 200
  deleteDetail = ''
  assignStatus = 200
  assignDetail = ''
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

  it('建错的组织能删掉', async () => {
    // 这一组功能存在的全部理由：建错一个组织，现在删不掉。
    const user = userEvent.setup()
    await renderPage()
    await screen.findByTestId('org-muji')

    await user.click(within(screen.getByTestId('org-muji')).getByRole('button', { name: '删除' }))

    await waitFor(() => {
      const del = requests.find((r) => r.method === 'DELETE')
      expect(del).toBeTruthy()
      expect(del!.url).toContain('/api/admin/organizations/muji')
    })
    await waitFor(() => expect(screen.queryByTestId('org-muji')).toBeNull())
  })

  it('名下还有租户时删不掉，把后端那句话原样显示', async () => {
    // 「还有 2 个租户，先把它们移出去」——比一句「删除失败」有用得多。
    deleteStatus = 409
    deleteDetail = "组织 'muji' 名下还有 2 个租户，先把它们移出去再删。"
    const user = userEvent.setup()
    await renderPage()
    await screen.findByTestId('org-muji')

    await user.click(within(screen.getByTestId('org-muji')).getByRole('button', { name: '删除' }))

    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('先把它们移出去'))
    // 组织还在——失败之后不能从界面上消失。
    expect(screen.getByTestId('org-muji')).toBeTruthy()
  })

  it('停用之后那个组织不能再被选中，但已经在里面的租户仍显示它', async () => {
    // 停用的组织从下拉框里整个删掉的话，已经挂在里面的租户会显示成「无」
    // ——看起来像被移出去了，而它其实还在里面。
    const user = userEvent.setup()
    await renderPage()
    await screen.findByTestId('org-muji')

    await user.click(within(screen.getByTestId('org-muji')).getByRole('button', { name: '停用' }))

    // 卡片标题上要标出来。「已停用」在下拉选项里也会出现，所以限定在标题里查。
    await waitFor(() =>
      expect(
        within(screen.getByTestId('org-muji')).getByRole('heading').textContent,
      ).toContain('已停用'),
    )
    const option = within(screen.getByTestId('org-unassigned')).getByRole('option', {
      name: /无印良品/,
    }) as HTMLOptionElement
    expect(option.disabled).toBe(true)
    // 已经在里面的那个仍然选中它。
    const inOrg = within(screen.getByTestId('org-muji')).getByLabelText(
      '导购小美 所属组织',
    ) as HTMLSelectElement
    expect(inOrg.value).toBe('muji')
  })

  it('停用的组织能再启用', async () => {
    // 反面：不给启用入口的话，停用就是一条单行道。
    const user = userEvent.setup()
    await renderPage()
    const org = await screen.findByTestId('org-muji')

    const heading = () => within(screen.getByTestId('org-muji')).getByRole('heading').textContent
    await user.click(within(org).getByRole('button', { name: '停用' }))
    await waitFor(() => expect(heading()).toContain('已停用'))
    await user.click(within(screen.getByTestId('org-muji')).getByRole('button', { name: '启用' }))

    await waitFor(() => expect(heading()).not.toContain('已停用'))
  })

  it('挂到停用的组织下被拒时，把后端那句话原样显示', async () => {
    assignStatus = 409
    assignDetail = "组织 'muji' 已停用，不能再往它下面挂租户。要用的话先启用它。"
    const user = userEvent.setup()
    await renderPage()
    const unassigned = await screen.findByTestId('org-unassigned')

    await user.selectOptions(within(unassigned).getByLabelText('演示 所属组织'), 'muji')

    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('已停用'))
  })
})
