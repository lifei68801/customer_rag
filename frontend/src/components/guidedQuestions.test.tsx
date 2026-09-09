import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from '../admin/SkinContext'
import { ConfirmProvider } from '../admin/ConfirmContext'
import { ToastProvider } from '../admin/ToastContext'
import { resetAdminSession } from '../admin/useAdminAuth'
import type { Persona, PersonaDetail } from '../lib/personasApi'

/**
 * 前台空会话时的引导问题。
 *
 * 这些用例走整个 App，不是单独渲染 GuidedQuestions——要测的东西
 * （空会话才出现、点了真的问出去、切数字人跟着换）没有一条只发生在
 * 组件内部，全都是组件和 ChatWorkspace 取数、useAgentChat 发问、
 * PersonaRail 切租户之间的接缝。
 */
const PERSONA: Persona = {
  tenant_id: 'muji-goods',
  name: '导购小美',
  avatar: '🛍️',
  tagline: '我知道商品、口味和产地',
}
const PERSONA_STORE: Persona = {
  tenant_id: 'muji-store',
  name: '店务老张',
  avatar: '🏪',
  tagline: '门店的事问我',
}

let personaDetail: PersonaDetail
let personaDetailStatus = 200
let chatRequests: { body: unknown }[] = []

function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/auth/whoami')) {
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
      // 数字人列表和数字人详情是两个不同的路径，且 includes('/persona')
      // 会把列表那条也吃进来。列表用精确前缀先分派，详情用正则限定
      // 「/api/admin/<一段租户>/persona」这个形状。
      if (url.includes('/api/admin/personas')) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              personas: [PERSONA, PERSONA_STORE],
              current_tenant_id: 'muji-goods',
            }),
            { status: 200 },
          ),
        )
      }
      if (/\/api\/admin\/[^/]+\/persona$/.test(url)) {
        return Promise.resolve(
          new Response(JSON.stringify(personaDetail), { status: personaDetailStatus }),
        )
      }
      if (url.includes('/api/admin/auth/session/tenant')) {
        const tenantId = JSON.parse(String(init?.body ?? '{}')).tenant_id as string
        return Promise.resolve(
          new Response(JSON.stringify({ tenant_id: tenantId }), { status: 200 }),
        )
      }
      if (url === '/agent/chat') {
        chatRequests.push({ body: init?.body ?? null })
        return Promise.resolve(
          new Response('data: {"type":"final","text":"好的"}\n\n', {
            status: 200,
            headers: { 'Content-Type': 'text/event-stream' },
          }),
        )
      }
      // 会话列表要精确匹配，不能用 includes('/agent/sessions')——那会连
      // /agent/sessions/{id}/messages 也一起吃掉。
      if (url === '/agent/sessions') {
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
  personaDetail = { ...PERSONA, questions: ['有哪些无香料的洗发水？', '哪些商品产自日本？'] }
  personaDetailStatus = 200
  chatRequests = []
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

describe('前台引导问题', () => {
  it('空会话时显示人设和引导问题', async () => {
    renderChat()
    await waitFor(() => expect(screen.getByText('我知道商品、口味和产地')).toBeTruthy())
    expect(screen.getByRole('button', { name: '有哪些无香料的洗发水？' })).toBeTruthy()
  })

  it('点一条引导问题就直接问出去', async () => {
    const user = userEvent.setup()
    renderChat()
    await waitFor(() => expect(screen.getByRole('button', { name: /无香料/ })).toBeTruthy())
    await user.click(screen.getByRole('button', { name: /无香料/ }))
    await waitFor(() =>
      expect(
        chatRequests.some((r) => JSON.parse(String(r.body)).question.includes('无香料')),
      ).toBe(true),
    )
  })

  it('已经有消息之后引导区消失', async () => {
    // 引导问题是「不知道能问什么」时的帮手。对话开始之后它占的是正文的位置。
    const user = userEvent.setup()
    renderChat()
    await waitFor(() => expect(screen.getByRole('button', { name: /无香料/ })).toBeTruthy())
    await user.click(screen.getByRole('button', { name: /无香料/ }))
    await waitFor(() => expect(screen.queryByRole('button', { name: /无香料/ })).toBeNull())
  })

  it('一条引导问题都没有时整块不渲染，不是渲染一个空标题', async () => {
    personaDetail = { ...PERSONA, questions: [] }
    renderChat()
    await waitFor(() => expect(screen.getByText(PERSONA.tagline)).toBeTruthy())
    expect(screen.queryByTestId('guided-questions')).toBeNull()
  })

  it('从没配过数字人的租户：人设和问题都空，整块一点位置都不占', async () => {
    // 后端对没配过 persona 的租户返回 tagline: ''（admin_personas_routes.py
    // 的 `(persona or {}).get("tagline", "")`），这是每个新租户的默认状态，
    // 不是极端情况。两边都空还渲染的话，正文顶上会多出一块只有内边距的
    // 空白，看起来像加载卡住了。
    personaDetail = { ...PERSONA, tagline: '', questions: [] }
    renderChat()
    // 引导块不渲染时没有可见文字能当"数据已到手"的信号，改成显式把挂载
    // 后的 effect 和微任务队列跑完再断言——不跑完的话断言跑在第一帧上，
    // 那时本来就什么都没有，判断条件改坏了照样绿。
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0))
    })
    // 断言外层容器本身不在，不能只断言里面没文字：去掉 `return null`
    // 之后剩下的是一个带内边距的空 div，文字断言照样绿（变异 E 验过）。
    expect(screen.queryByTestId('guided-block')).toBeNull()
    expect(screen.queryByTestId('guided-questions')).toBeNull()
  })

  it('切换数字人之后引导问题换成新那个的', async () => {
    // 每个数字人守一张不同的图，引导问题必须跟着换。不换的话，用户在
    // 店务老张那里看到导购小美的问题，点了必然答不出来。
    const user = userEvent.setup()
    renderChat()
    await waitFor(() => expect(screen.getByRole('button', { name: /无香料/ })).toBeTruthy())
    personaDetail = { ...PERSONA_STORE, questions: ['哪家店卖得最好？'] }
    await user.click(screen.getByRole('button', { name: /店务老张/ }))
    await waitFor(() => expect(screen.getByRole('button', { name: /卖得最好/ })).toBeTruthy())
    // 只断言新的出现是假绿：「两批都渲染」的实现照样能过。
    expect(screen.queryByRole('button', { name: /无香料/ })).toBeNull()
  })

  it('详情拉取失败时说出来，不是静默地什么都不显示', async () => {
    // 「这个数字人没配引导问题」和「引导问题没拉回来」在界面上长得一样，
    // 而后者是用户该报修的故障。
    personaDetailStatus = 500
    renderChat()
    await waitFor(() => expect(screen.getByText(/数字人信息加载失败/)).toBeTruthy())
  })
})
