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
 * 默认身份是 admin；「member 身份下点另一个数字人同样会发切租户请求」那条
 * 用例把 signedInRole 切成 member。此前 TenantContext.setTenantId 对
 * member 硬编码 no-op，而右栏最主要的使用者恰恰是被授权访问多个数字人的
 * member 账号——那道闸门已经在这次改动里拆掉（见 admin/TenantContext.tsx
 * 和 admin/roleBasedMenu.test.tsx 的改动），这里补的这条用例是那个修复的
 * 回归覆盖。
 */
let signedInRole: 'admin' | 'member' = 'admin'
// 大多数用例里当前租户就是 'muji-goods'；Important 1 的回归用例需要模拟
// 「当前挂着的租户不在这个账号的授权列表里」，靠这个变量单独改写 whoami
// 返回的 current_tenant_id，不用碰 signedInRole。
let currentTenantIdOverride = 'muji-goods'

function whoamiResponse() {
  return Promise.resolve(
    new Response(
      JSON.stringify({
        username: signedInRole === 'admin' ? 'admin' : 'alice',
        role: signedInRole,
        tenant_id: signedInRole === 'admin' ? null : 'muji-goods',
        current_tenant_id: currentTenantIdOverride,
      }),
      { status: 200 },
    ),
  )
}

const ONE_PERSONA: Persona = {
  tenant_id: 'muji-goods',
  persona_id: 'default',
  name: '导购小美',
  avatar: '🛍️',
  tagline: '导购的事问我',
}

const TWO_PERSONAS: Persona[] = [
  ONE_PERSONA,
  {
    tenant_id: 'muji-store',
    persona_id: 'default',
    name: '店务老张',
    avatar: '🏪',
    tagline: '门店的事问我',
  },
]

/** 同一个租户下的第二张脸（ADR-0004）。 */
const SECOND_FACE_SAME_TENANT: Persona = {
  tenant_id: 'muji-goods',
  persona_id: 'kefu',
  name: '客服阿May',
  avatar: '🎧',
  tagline: '售后的事问我',
}

let personasResponse: { personas: Persona[]; current_tenant_id: string | null }
let personasStatus = 200
let switchRequests: { method: string; body: unknown }[] = []
let sessionsRequestCount = 0
let sessionsRequestedPersonas: (string | null)[] = []

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
      // 数字人详情：ChatWorkspace 对 (当前租户, 当前脸) 拉一次。这里按
      // persona_id 回对应那张脸，不是任由它落进末尾那个永不 resolve 的
      // 兜底——挂着的请求会让这些用例在「详情加载失败该说话」这类回归上
      // 瞎掉；回错脸的话「切脸之后一句话换了」那条就看不到换。
      if (/\/api\/admin\/[^/]+\/persona(\?|$)/.test(url)) {
        const requested = new URL(url, 'http://x').searchParams.get('persona_id') ?? 'default'
        const face =
          personasResponse.personas.find((p) => p.persona_id === requested) ?? ONE_PERSONA
        return Promise.resolve(
          new Response(JSON.stringify({ ...face, questions: [] }), { status: 200 }),
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
      if (url === '/agent/sessions' || url.startsWith('/agent/sessions?')) {
        sessionsRequestCount += 1
        sessionsRequestedPersonas.push(new URL(url, 'http://x').searchParams.get('persona_id'))
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
  signedInRole = 'admin'
  currentTenantIdOverride = 'muji-goods'
  personasResponse = { personas: TWO_PERSONAS, current_tenant_id: 'muji-goods' }
  personasStatus = 200
  switchRequests = []
  sessionsRequestedPersonas = []
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

  it('member 身份下点另一个数字人同样会发切租户请求', async () => {
    // 右栏最主要的使用者是被授权访问多个数字人的 member 账号，不是 admin
    // ——admin 本来就不设限。把 TenantContext.tsx 里 `if (role !== 'admin')
    // return` 加回去会让这条变红，其它用例不受影响（见变异记录）。
    signedInRole = 'member'
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

  it('同一个租户下切脸，不发切租户的请求', async () => {
    // 今天 onSelect={setTenantId}，切脸会 PUT 一次当前租户。同租户内切脸
    // 不该动租户——发了的话，一次纯展示的切换会连带刷新整个知识库上下文，
    // 而脸是展示单元、租户才是隔离单元（ADR-0004）。
    personasResponse = {
      personas: [ONE_PERSONA, SECOND_FACE_SAME_TENANT],
      current_tenant_id: 'muji-goods',
    }
    const user = userEvent.setup()
    renderChat()
    await waitFor(() => expect(screen.getByText('客服阿May')).toBeTruthy())

    await user.click(screen.getByRole('button', { name: /客服阿May/ }))

    // 脸换了（一句话换了），但一个切租户的 PUT 都没发。
    await waitFor(() => expect(screen.getByText(/售后的事问我/)).toBeTruthy())
    expect(switchRequests.filter((r) => r.method === 'PUT')).toEqual([])
  })

  it('跨租户切脸，仍然要先切租户', async () => {
    // 反面：不切的话，用户点了别的租户的脸，问答还在原租户的知识里跑。
    // 上面那条在「一律不切租户」的实现下也是绿的，这条把它挡住。
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

  it('切脸之后左栏会话列表按那张脸重拉', async () => {
    // 一个会话属于一个数字人。切脸不重拉的话，左栏挂着的是上一张脸的会话。
    personasResponse = {
      personas: [ONE_PERSONA, SECOND_FACE_SAME_TENANT],
      current_tenant_id: 'muji-goods',
    }
    const user = userEvent.setup()
    renderChat()
    await waitFor(() => expect(screen.getByText('客服阿May')).toBeTruthy())
    await waitFor(() => expect(sessionsRequestCount).toBeGreaterThan(0))

    await user.click(screen.getByRole('button', { name: /客服阿May/ }))

    await waitFor(() => expect(sessionsRequestedPersonas).toContain('kefu'))
  })

  it('右栏用 (租户, 脸) 两段标识，同一个租户的两张脸各是一项', async () => {
    // 只用 tenant_id 当 key 的话，同一个租户的两张脸会被 React 当成同一项。
    // 两个按钮照样都渲染出来（React 只是警告，不吞元素），所以数按钮抓不到
    // 这个回归——要抓的是那条 "Encountered two children with the same key"
    // 警告本身：它是 key 撞车唯一可观察的后果，也是重排/切脸时错位的前兆。
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    personasResponse = {
      personas: [ONE_PERSONA, SECOND_FACE_SAME_TENANT],
      current_tenant_id: 'muji-goods',
    }
    renderChat()

    await waitFor(() => expect(screen.getByText('客服阿May')).toBeTruthy())
    expect(screen.getByText('导购小美')).toBeTruthy()
    expect(screen.getAllByRole('button', { name: /导购小美|客服阿May/ })).toHaveLength(2)
    const duplicateKeyWarnings = consoleError.mock.calls.filter((args) =>
      args.some((a) => typeof a === 'string' && a.includes('same key')),
    )
    consoleError.mockRestore()
    expect(duplicateKeyWarnings).toEqual([])
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
        { tenant_id: 'muji-goods', persona_id: 'default', name: '商品', avatar: '', tagline: '' },
        { tenant_id: 'muji-store', persona_id: 'default', name: '门店', avatar: '🏪', tagline: '门店的事问我' },
      ],
      current_tenant_id: 'muji-goods',
    }
    renderChat()
    await waitFor(() => expect(screen.getByText('商品')).toBeTruthy())
    // 断言占位符本身，不能只断言名字——`persona.avatar || '◍'` 改回
    // `persona.avatar` 之后名字照样渲染，这条不认那个回归就是假绿。
    expect(screen.getByText('◍')).toBeTruthy()
  })

  it('当前租户不在授权列表里时，哪怕只有一个数字人也要渲染右栏——那是唯一的自救入口', async () => {
    // 复现 2026-09-08 评审 Important 1：账号被授权了 muji-store，但会话
    // 当前挂着的 current_tenant_id 还是回退值 muji-goods（不在授权范围
    // 内）。personas.length === 1 时旧判断 `personas.length <= 1` 会把
    // 唯一能纠正的入口也藏起来，人从此看得见 403、纠正不了。
    currentTenantIdOverride = 'muji-goods'
    personasResponse = {
      personas: [{ tenant_id: 'muji-store', persona_id: 'default', name: '店务老张', avatar: '🏪', tagline: '门店的事问我' }],
      current_tenant_id: 'muji-goods',
    }
    renderChat()
    await waitFor(() => expect(screen.getByText('店务老张')).toBeTruthy())
    expect(screen.getByRole('complementary', { name: '数字人' })).toBeTruthy()
    // 正文区要说清楚发生了什么，不能只剩一串看不懂的 403。
    expect(screen.getByText('当前知识库你没有权限访问')).toBeTruthy()
  })
})
