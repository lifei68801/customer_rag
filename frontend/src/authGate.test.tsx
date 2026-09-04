import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from './App'
import { SkinProvider } from './admin/SkinContext'
import { ConfirmProvider } from './admin/ConfirmContext'
import { ToastProvider } from './admin/ToastContext'
import { resetAdminSession } from './admin/useAdminAuth'

/**
 * 前台（`/`）的登录门与账号块。
 *
 * 前台此前完全没有身份：租户是硬编码的 'demo'，用户是 localStorage 里的
 * 随机 UUID。服务端五个前台接口现在都要会话，前台不登录就是全线 401。
 *
 * 身份从 whoami 拿（token 在 HttpOnly Cookie 里，JS 读不到也塞不进去），
 * 所以这里要打桩的是 whoami。
 */

interface Whoami {
  username: string
  role: 'admin' | 'member'
  tenant_id: string | null
  current_tenant_id: string | null
}

function stubApi({
  whoami,
  tenants = [{ tenant_id: 'demo', name: 'demo', status: 'active' }],
}: {
  whoami: Whoami | 401
  tenants?: { tenant_id: string; name: string; status: string }[]
}) {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/auth/whoami')) {
        return Promise.resolve(
          whoami === 401
            ? new Response('{}', { status: 401 })
            : new Response(JSON.stringify(whoami), { status: 200 }),
        )
      }
      if (url.includes('/auth/session/tenant')) {
        return Promise.resolve(new Response('{}', { status: 200 }))
      }
      if (url.includes('/api/admin/tenants')) {
        return Promise.resolve(new Response(JSON.stringify({ tenants }), { status: 200 }))
      }
      if (url.includes('/agent/sessions')) {
        return Promise.resolve(new Response(JSON.stringify({ sessions: [] }), { status: 200 }))
      }
      return new Promise<Response>(() => {})
    }),
  )
}

beforeEach(() => {
  // 会话状态在模块级，同一个文件里跨用例存活——不重置的话上一条用例
  // 登录出来的身份会漏进下一条。
  resetAdminSession()
  localStorage.clear()
})

function renderAt(path: string) {
  // 这三个 Provider 挂在 main.tsx 的根节点（站点级能力，前台后台共用），
  // 不在 App 内部，所以测试要自己补上。
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

describe('前台登录门', () => {
  it('未登录时前台渲染登录表单，不渲染问答界面', async () => {
    stubApi({ whoami: 401 })
    renderAt('/')
    expect(await screen.findByLabelText(/用户名/)).toBeTruthy()
    expect(screen.queryByPlaceholderText(/输入你的问题/)).toBeNull()
  })
})

describe('前台账号块', () => {
  it('登录后前台显示账号块，但没有账号管理和租户管理', async () => {
    // 前台是「用知识库」的地方，后台是「管知识库」的地方。把管理入口塞进
    // 问答界面，等于把建模→接入→审核这条流程的入口散回一个不属于它的页面。
    stubApi({
      whoami: { username: 'admin', role: 'admin', tenant_id: null, current_tenant_id: 'demo' },
    })
    renderAt('/')
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: /admin/ }))
    expect(screen.getByRole('menuitem', { name: '设置' })).toBeTruthy()
    expect(screen.queryByRole('menuitem', { name: '账号管理' })).toBeNull()
    expect(screen.queryByRole('menuitem', { name: '租户管理' })).toBeNull()
  })

  it('前台给 admin 显示租户切换器', async () => {
    // 换租户即换知识库，admin 需要验证「我刚配好的本体，问答到底通不通」。
    stubApi({
      whoami: { username: 'admin', role: 'admin', tenant_id: null, current_tenant_id: 'demo' },
    })
    renderAt('/')
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: /admin/ }))
    expect(await screen.findByRole('menuitemradio', { name: /demo/ })).toBeTruthy()
  })

  it('member 看不到租户切换器', async () => {
    stubApi({
      whoami: { username: 'alice', role: 'member', tenant_id: 'demo', current_tenant_id: 'demo' },
    })
    renderAt('/')
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: /alice/ }))
    expect(screen.queryByRole('menuitemradio')).toBeNull()
  })
})

describe('还没选租户', () => {
  it('把「请先选择一个租户」摆出来，而不是一片什么都没有的问答界面', async () => {
    // admin 的 tenant_id 恒为 None，当前租户要显式切过一次才有值。在那
    // 之前前台每个请求都会撞上后端的 400「请先选择一个租户」。
    //
    // 租户列表给空的：useTenants 的「当前租户不在列表里就自动纠正」只在
    // 列表非空时才动得了，所以这里没有东西会替用户把它补上——正是需要
    // 用户自己看见并纠正的那个状态。
    stubApi({
      whoami: { username: 'admin', role: 'admin', tenant_id: null, current_tenant_id: null },
      tenants: [],
    })
    renderAt('/')

    expect(await screen.findByText(/请先选择一个租户/)).toBeTruthy()
    expect(screen.queryByPlaceholderText(/输入你的问题/)).toBeNull()
    // 光说「没选」不够：纠正它的地方必须同时在屏幕上。
    expect(screen.getByRole('button', { name: /admin/ })).toBeTruthy()
  })
})
