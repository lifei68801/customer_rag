import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from '../admin/SkinContext'
import { ConfirmProvider } from '../admin/ConfirmContext'
import { ToastProvider } from '../admin/ToastContext'
import { resetAdminSession } from '../admin/useAdminAuth'
import type { Persona } from '../lib/personasApi'

/**
 * 前台右栏：这个账号能访问的数字人。
 *
 * 「数字人」不是新概念——租户就是知识库这件事今天已经存在（NoTenantNotice
 * 的空态原文就是这句话），右栏只是把藏在左下角账号块下拉框里的租户切换器
 * 升级成常驻可见的一栏。
 *
 * whoami 里给 admin：TenantContext.setTenantId 目前对 member 是硬编码的
 * no-op（role !== 'admin' 直接 return，见 admin/roleBasedMenu.test.tsx 里
 * 「setTenantId 不生效」那条用例钉住的行为）。这跟 personas 端点明确把
 * member 也纳入服务范围是矛盾的，但那个矛盾不在本任务改动的文件列表里，
 * 这里用 admin 身份绕开它，不代表这个矛盾不存在——见任务报告里的顾虑。
 */
function whoamiResponse() {
  return Promise.resolve(
    new Response(
      JSON.stringify({
        username: 'admin',
        role: 'admin',
        tenant_id: null,
        current_tenant_id: 'muji-goods',
      }),
      { status: 200 },
    ),
  )
}

const ONE_PERSONA: Persona = {
  tenant_id: 'muji-goods',
  name: '导购小美',
  avatar: '🛍️',
  tagline: '导购的事问我',
}

const TWO_PERSONAS: Persona[] = [
  ONE_PERSONA,
  { tenant_id: 'muji-store', name: '店务老张', avatar: '🏪', tagline: '门店的事问我' },
]

let personasResponse: { personas: Persona[]; current_tenant_id: string | null }
let personasStatus = 200
let switchRequests: { method: string; body: unknown }[] = []
let sessionsRequestCount = 0

function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/auth/whoami')) return whoamiResponse()
      // 分派顺序：这条必须排在任何 '/api/admin' 兜底之前，否则会被吃掉。
      if (url.includes('/api/admin/personas')) {
        return Promise.resolve(
          new Response(JSON.stringify(personasResponse), { status: personasStatus }),
        )
      }
      if (url.includes('/api/admin/auth/session/tenant')) {
        switchRequests.push({
          method: (init?.method ?? 'GET').toUpperCase(),
          body: init?.body ?? null,
        })
        const tenantId = JSON.parse(String(init?.body ?? '{}')).tenant_id as string
        return Promise.resolve(
          new Response(JSON.stringify({ tenant_id: tenantId }), { status: 200 }),
        )
      }
      // 会话列表：精确匹配，不能用 includes('/agent/sessions')——那会连
      // /agent/sessions/{id}/messages 也一起吃掉，把消息接口的响应形状
      // 换成 { sessions: [] }。
      if (url === '/agent/sessions') {
        sessionsRequestCount += 1
        return Promise.resolve(new Response(JSON.stringify({ sessions: [] }), { status: 200 }))
      }
      if (url.includes('/agent/sessions/')) {
        return Promise.resolve(new Response(JSON.stringify({ messages: [] }), { status: 200 }))
      }
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  personasResponse = { personas: TWO_PERSONAS, current_tenant_id: 'muji-goods' }
  personasStatus = 200
  switchRequests = []
  sessionsRequestCount = 0
  resetAdminSession()
  localStorage.clear()
  stubApi()
})

function renderChat() {
  return render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={['/']}>
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
}

describe('数字人右栏', () => {
  it('列出这个账号能访问的数字人，当前那个是选中态', async () => {
    renderChat()
    await waitFor(() => expect(screen.getByText('导购小美')).toBeTruthy())
    expect(screen.getByText('店务老张')).toBeTruthy()
    const active = screen.getByRole('button', { name: /导购小美/ })
    expect(active.getAttribute('aria-current')).toBe('true')
    // 非当前那个不能也是 aria-current，否则「哪个是当前」这件事就没说清楚
    expect(
      screen.getByRole('button', { name: /店务老张/ }).getAttribute('aria-current'),
    ).not.toBe('true')
  })

  it('点另一个数字人会发切租户请求', async () => {
    const user = userEvent.setup()
    renderChat()
    await waitFor(() => expect(screen.getByText('店务老张')).toBeTruthy())
    await user.click(screen.getByRole('button', { name: /店务老张/ }))
    await waitFor(() =>
      expect(
        switchRequests.some(
          (r) => r.method === 'PUT' && JSON.parse(String(r.body)).tenant_id === 'muji-store',
        ),
      ).toBe(true),
    )
  })

  it('切换之后会话历史跟着换成新数字人的', async () => {
    // chat_sessions 主键是 (tenant_id, session_id)，切租户后 GET /sessions
    // 必须重新拉。不重拉的话左栏还挂着上一个数字人的会话，点进去是空的。
    const user = userEvent.setup()
    renderChat()
    await waitFor(() => expect(screen.getByText('店务老张')).toBeTruthy())
    await waitFor(() => expect(sessionsRequestCount).toBeGreaterThan(0))
    sessionsRequestCount = 0
    await user.click(screen.getByRole('button', { name: /店务老张/ }))
    await waitFor(() => expect(sessionsRequestCount).toBeGreaterThan(0))
  })

  it('只有一个数字人时右栏不出现', async () => {
    // 一个选项的选择器不是选择器，是噪音。它还会误导用户以为「还有别的，
    // 只是我没权限」——而实际上这个部署就只有一个知识库。
    //
    // 只有一个数字人时 PersonaRail 在加载中和加载完成两种状态下都不渲染
    // 任何东西，所以没有一处可见文字能当作"数据已经拉回来了"的信号去
    // waitFor。改成显式把挂载后的 effect 和微任务队列跑完（这个仓库
    // sessionExpiry.test.tsx 里同样的手法），再断言——不跑完的话断言就是
    // 跑在第一帧（加载中，右栏本来就是 null）上，personas.length <= 0
    // 那种改坏了判断条件的实现照样能让它绿。
    personasResponse = { personas: [ONE_PERSONA], current_tenant_id: 'muji-goods' }
    renderChat()
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0))
    })
    expect(screen.queryByRole('complementary', { name: '数字人' })).toBeNull()
  })

  it('拉取失败时说出来，不是静默空栏', async () => {
    // 空栏和「拉取失败」在界面上长得一样，而它们要用户做的事完全不同。
    personasStatus = 500
    renderChat()
    await waitFor(() => expect(screen.getByText(/数字人列表加载失败/)).toBeTruthy())
  })

  it('没配脸的数字人显示租户名和一个占位头像，不是空白', async () => {
    personasResponse = {
      personas: [
        { tenant_id: 'muji-goods', name: '商品', avatar: '', tagline: '' },
        { tenant_id: 'muji-store', name: '门店', avatar: '🏪', tagline: '门店的事问我' },
      ],
      current_tenant_id: 'muji-goods',
    }
    renderChat()
    await waitFor(() => expect(screen.getByText('商品')).toBeTruthy())
  })
})
