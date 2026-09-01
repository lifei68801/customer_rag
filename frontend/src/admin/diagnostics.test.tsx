import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { ADMIN_ROUTES } from '../adminRoutes'

/**
 * 问答诊断页。
 *
 * 这是「答错了 → 哪个实体不对」这条路的中间一跳。选一次真实的错误回答，
 * 看它用了哪些工具、匹配到哪些实体，每个实体链到详情页。
 *
 * 输入是历史会话而不是手动重跑：LLM 非确定性，重跑可能复现不出那个错误，
 * 你会对着一个正确的结果找不到问题。
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
  used_sources: ['faq/drinks.md'],
  created_at: '2026-09-01 10:00:00',
  tool_results: [
    {
      tool_call_id: 'c1',
      name: 'structured_filter_query_tool',
      content: '{"matched_count":2,"anchors":[{"node_key":"公司:可口可乐"}]}',
    },
  ],
  mentioned_terms: [
    { node_key: '公司:可口可乐', standard_name: '可口可乐', term_type: '公司' },
    { node_key: '产品:雪碧', standard_name: '雪碧', term_type: '产品' },
  ],
}

function stubApi(detail: unknown = DETAIL) {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (/\/diagnostics\/\d+/.test(url)) {
        return Promise.resolve(new Response(JSON.stringify(detail), { status: 200 }))
      }
      if (url.includes('/diagnostics')) {
        return Promise.resolve(new Response(JSON.stringify(LIST), { status: 200 }))
      }
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  sessionStorage.setItem('admin_session_token', 'test-token')
  sessionStorage.setItem('admin_current_tenant', 'demo')
  localStorage.clear()
  stubApi()
})

function renderAt(path = ADMIN_ROUTES.diagnostics) {
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

const page = () => within(screen.getByTestId('diagnostics'))

describe('历史列表', () => {
  it('最近的问答排在最前面', async () => {
    renderAt()
    await waitFor(() => expect(page().getByText('可口可乐有哪些产品')).toBeTruthy())
    const questions = page()
      .getAllByRole('button', { name: /可口可乐|订单 123/ })
      .map((b) => b.textContent ?? '')
    expect(questions[0]).toContain('可口可乐')
  })

  it('列表上就能看到答案——不点开也能认出是哪一次答错的', async () => {
    renderAt()
    await waitFor(() => expect(page().getByText(/找不到/)).toBeTruthy())
  })
})

describe('诊断详情', () => {
  it('点一次问答，列出它碰到的实体，每个链到详情页', async () => {
    const user = userEvent.setup()
    renderAt()
    await waitFor(() => expect(page().getByText('可口可乐有哪些产品')).toBeTruthy())
    await user.click(page().getByRole('button', { name: /可口可乐有哪些产品/ }))

    await waitFor(() => expect(page().getByRole('link', { name: /可口可乐/ })).toBeTruthy())
    expect(page().getByRole('link', { name: /可口可乐/ }).getAttribute('href')).toBe(
      `/admin/terms/${encodeURIComponent('公司:可口可乐')}`,
    )
    expect(page().getByRole('link', { name: /雪碧/ })).toBeTruthy()
  })

  it('显示用了哪些工具', async () => {
    const user = userEvent.setup()
    renderAt()
    await waitFor(() => expect(page().getByText('可口可乐有哪些产品')).toBeTruthy())
    await user.click(page().getByRole('button', { name: /可口可乐有哪些产品/ }))

    await waitFor(() => expect(page().getByText('structured_filter_query_tool')).toBeTruthy())
  })

  it('一个实体都没碰到时，明说这次没用上图谱', async () => {
    // 这不是「暂无数据」，是个结论：这次走的是纯向量检索。如果问题恰恰
    // 出在图谱上，这一句就是答案。
    stubApi({ ...DETAIL, mentioned_terms: [] })
    const user = userEvent.setup()
    renderAt()
    await waitFor(() => expect(page().getByText('可口可乐有哪些产品')).toBeTruthy())
    await user.click(page().getByRole('button', { name: /可口可乐有哪些产品/ }))

    await waitFor(() => expect(page().getByText(/没有用到图谱|纯向量检索/)).toBeTruthy())
  })

  it('被截断的工具结果要标出来', async () => {
    // 排查的人看到一段结果会默认那就是全部，据此得出「只匹配到 3 条」
    // 这样的结论。
    stubApi({
      ...DETAIL,
      tool_results: [{ ...DETAIL.tool_results[0], content_truncated: true }],
    })
    const user = userEvent.setup()
    renderAt()
    await waitFor(() => expect(page().getByText('可口可乐有哪些产品')).toBeTruthy())
    await user.click(page().getByRole('button', { name: /可口可乐有哪些产品/ }))

    await waitFor(() => expect(page().getByText(/已截断/)).toBeTruthy())
  })
})

describe('还没有记录', () => {
  it('说明诊断是从这之后才开始记的', async () => {
    // 空列表在这里有个特殊含义：功能刚上线，历史问答没有快照。不说清楚
    // 的话，用户会以为是坏了。
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        if (String(input).includes('/diagnostics')) {
          return Promise.resolve(new Response(JSON.stringify({ diagnostics: [] }), { status: 200 }))
        }
        return new Promise(() => {})
      }),
    )
    renderAt()
    await waitFor(() => expect(page().getByText(/还没有/)).toBeTruthy())
  })
})
