import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { ADMIN_ROUTES } from '../adminRoutes'
import { resetAdminSession } from './useAdminAuth'

/**
 * 身份不再存 sessionStorage（token 在 HttpOnly Cookie 里，JS 读不到，也
 * 塞不进去）：界面从 whoami 拿身份，所以这里要打桩的是 whoami。
 */
function whoamiResponse() {
  return Promise.resolve(
    new Response(
      JSON.stringify({
        username: 'alice',
        role: 'member',
        tenant_id: 'demo',
        current_tenant_id: 'demo',
      }),
      { status: 200 },
    ),
  )
}

/**
 * 「答错了 → 哪个实体不对」这条路上的返回键。
 *
 * 实体详情页此前不管从哪来，返回链接都写死指向实体列表。从问答诊断页
 * 点进来的人想回的是那次诊断——回到列表等于把整个调查现场丢了，而且
 * 要重新在几十条问答里找回刚才那一条。
 *
 * 前提是那一条得有地址：诊断页选中哪条此前只活在组件 state 里，连它
 * 自己刷新一下都回不去。
 */

const LIST = {
  diagnostics: [
    { id: 2, session_id: 's1', question: '可口可乐有哪些产品', answer: '雪碧、芬达。', created_at: '2026-09-01 10:00:00' },
    { id: 1, session_id: 's1', question: '订单 123 的金额', answer: '找不到。', created_at: '2026-09-01 09:00:00' },
  ],
}

const DETAIL = {
  id: 2,
  session_id: 's1',
  question: '可口可乐有哪些产品',
  resolved_question: '可口可乐有哪些产品',
  answer: '雪碧、芬达。',
  used_sources: [],
  created_at: '2026-09-01 10:00:00',
  tool_results: [],
  mentioned_terms: [{ node_key: '公司:可口可乐', standard_name: '可口可乐', term_type: '公司' }],
}

const TERM = {
  node_key: '公司:可口可乐',
  standard_name: '可口可乐',
  aliases: [],
  term_type: '公司',
  extra_properties: {},
  source: 'etl',
  relations: [
    { direction: 'out', relation_type: '生产', node_key: '产品:雪碧', standard_name: '雪碧', term_type: '产品' },
  ],
}

const SPRITE = { ...TERM, node_key: '产品:雪碧', standard_name: '雪碧', term_type: '产品', relations: [] }

function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/auth/whoami')) return whoamiResponse()
      const json = (body: unknown) =>
        Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))
      if (/\/diagnostics\/\d+/.test(url)) return json(DETAIL)
      if (url.includes('/diagnostics')) return json(LIST)
      if (url.includes(encodeURIComponent('产品:雪碧'))) return json(SPRITE)
      if (url.includes('/terms/summary')) return json({ groups: [] })
      if (url.includes('/terms/')) return json(TERM)
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  resetAdminSession()
  localStorage.clear()
  stubApi()
})

function Probe() {
  const { pathname, search } = useLocation()
  return <span data-testid="url">{pathname + search}</span>
}

function renderAt(path: string) {
  return render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[path]}>
            <Probe />
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
}

const url = () => screen.getByTestId('url').textContent
const backLink = () => within(screen.getByTestId('term-detail')).getByTestId('term-back-link')

describe('诊断页选中哪一条要有地址', () => {
  it('选中后写进 URL', async () => {
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.diagnostics)
    await user.click(await screen.findByRole('button', { name: /可口可乐有哪些产品/ }))
    expect(url()).toBe(`${ADMIN_ROUTES.diagnostics}?d=2`)
  })

  it('带着地址直接打开，详情就是展开的——刷新和分享都回得去', async () => {
    renderAt(`${ADMIN_ROUTES.diagnostics}?d=2`)
    // 详情区独有的小标题；列表本身不含它。
    expect(await screen.findByText('这次用到了什么')).toBeTruthy()
  })
})

describe('实体详情页的返回键跟着来路走', () => {
  it('从诊断页进来：回到那一条诊断，不是回列表', async () => {
    const user = userEvent.setup()
    renderAt(`${ADMIN_ROUTES.diagnostics}?d=2`)
    await user.click(await screen.findByRole('link', { name: /可口可乐/ }))
    await waitFor(() => expect(screen.getByTestId('term-detail')).toBeTruthy())
    expect(backLink().getAttribute('href')).toBe(`${ADMIN_ROUTES.diagnostics}?d=2`)
    expect(backLink().textContent).toMatch(/问答诊断/)
  })

  it('沿着关系再跳一个实体，返回键仍指向诊断——链式浏览不该丢掉起点', async () => {
    const user = userEvent.setup()
    renderAt(`${ADMIN_ROUTES.diagnostics}?d=2`)
    await user.click(await screen.findByRole('link', { name: /可口可乐/ }))
    await waitFor(() => expect(screen.getByTestId('term-detail')).toBeTruthy())
    await user.click(screen.getByRole('link', { name: '雪碧' }))
    await waitFor(() => expect(url()).toContain(encodeURIComponent('产品:雪碧')))
    expect(backLink().getAttribute('href')).toBe(`${ADMIN_ROUTES.diagnostics}?d=2`)
  })

  it('直接打开详情页（没有来路）：回实体列表', async () => {
    renderAt(`${ADMIN_ROUTES.terms}/${encodeURIComponent('公司:可口可乐')}`)
    await waitFor(() => expect(screen.getByTestId('term-detail')).toBeTruthy())
    // 分享出去的链接不带来路，退回默认去处，而不是没有出口。
    expect(backLink().getAttribute('href')).toBe(ADMIN_ROUTES.terms)
    expect(backLink().textContent).toMatch(/实体列表/)
  })
})
