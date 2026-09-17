import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../../../App'
import { SkinProvider } from '../../SkinContext'
import { ConfirmProvider } from '../../ConfirmContext'
import { ToastProvider } from '../../ToastContext'
import { modelingWay } from '../../../adminRoutes'
import { resetAdminSession } from '../../useAdminAuth'

/**
 * 智能创建面板的行为测试。跟 workbenchPage.test.tsx 同一套搭台方式（whoami
 * 打桩、整个 App 挂在 MemoryRouter 里走真实路由到 modelingWay('smart')）。
 */
let signedInRole: 'admin' | 'member' | null = null
let session: unknown = null
const saved: { state: { skeleton: { term_types: { value: string; review: string }[] } } }[] = []
const draftReplaceBodies: unknown[] = []
let answerResult: { session: unknown; turn: unknown } | null = null
let questionResult: { session: unknown; question: unknown } | null = null
let applyPreviewRemovedTermTypes: string[] = []

function openingSession() {
  return {
    tenant_id: 'demo',
    updated_at: '2026-09-17T10:00:00',
    updated_by: 'alice',
    state: {
      turns: [{ role: 'assistant', text: '先说说你们主要做什么生意？' }],
      skeleton: { term_types: [], relation_types: [], constraints: [] },
      questions: [],
      done: false,
    },
  }
}

/** 已经有一个候选实体「商品」的会话，给"接受"、"写入草稿"这类测试起步用。 */
function sessionWithTerm(review: 'pending' | 'accepted') {
  return {
    tenant_id: 'demo',
    updated_at: '2026-09-17T10:00:00',
    updated_by: 'alice',
    state: {
      turns: [
        { role: 'assistant', text: '先说说你们主要做什么生意？' },
        { role: 'user', text: '我们主要卖服装和家居' },
      ],
      skeleton: {
        term_types: [
          {
            value: '商品',
            display_name: '商品',
            rationale: '用户说主要卖服装和家居，商品是最小的售卖单位',
            confidence: 'guess',
            from_turn: 1,
            review,
            extra_fields: [],
            standard_name_value_type: 'string',
          },
        ],
        relation_types: [],
        constraints: [],
      },
      questions: [],
      done: false,
    },
  }
}

const json = (body: unknown, status = 200) =>
  Promise.resolve(new Response(JSON.stringify(body), { status }))

function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      if (url.includes('/auth/whoami')) {
        if (signedInRole === null) return json({ detail: '未登录' }, 401)
        return json({
          username: 'alice',
          role: signedInRole,
          tenant_id: signedInRole === 'admin' ? null : 'demo',
          current_tenant_id: 'demo',
        })
      }
      if (url.includes('/nav-badges')) {
        return json({ pending_relations: 0, pending_duplicates: 0, total_terms: 0 })
      }
      if (url.includes('/interview/answer')) {
        return json(
          answerResult ?? {
            session,
            turn: { question: null, added_count: 0, dropped: [], note: null },
          },
        )
      }
      if (url.includes('/interview/questions')) {
        return json(
          questionResult ?? {
            session,
            question: { text: '', needs: { term_types: [], relation_types: [] }, missing: [], at: 'now' },
          },
        )
      }
      if (url.includes('/modeling-workspace/apply-preview')) {
        return json({
          added_term_types: [],
          removed_term_types: applyPreviewRemovedTermTypes,
          changed_term_types: [],
          added_relation_types: [],
          removed_relation_types: [],
          added_constraints: [],
          removed_constraints: [],
        })
      }
      if (url.includes('/draft/replace')) {
        const body = JSON.parse(String(init?.body))
        draftReplaceBodies.push(body)
        return json({ replaced: true })
      }
      if (url.includes('/interview')) {
        if (method === 'GET') return json({ session })
        if (method === 'POST') {
          session = openingSession()
          return json({ session })
        }
        if (method === 'PUT') {
          const body = JSON.parse(String(init?.body)) as {
            state: { skeleton: { term_types: { value: string; review: string }[] } }
          }
          saved.push(body)
          session = { ...(session as object), state: body.state, updated_at: 'later' }
          return json({ session })
        }
        if (method === 'DELETE') {
          session = null
          return json({ deleted: true })
        }
        return json({ session })
      }
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  signedInRole = null
  session = null
  saved.length = 0
  draftReplaceBodies.length = 0
  answerResult = null
  questionResult = null
  applyPreviewRemovedTermTypes = []
  resetAdminSession()
  sessionStorage.clear()
  localStorage.clear()
  stubApi()
})

function renderSmart() {
  return render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[modelingWay('smart')]}>
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
}

describe('智能创建', () => {
  it('没有会话时显示「开始访谈」，点了之后出现开场问题', async () => {
    signedInRole = 'member'
    renderSmart()
    await userEvent.click(await screen.findByRole('button', { name: '开始访谈' }))
    expect(await screen.findByText('先说说你们主要做什么生意？')).toBeInTheDocument()
  })

  it('回答一轮后骨架区长出新的候选实体', async () => {
    signedInRole = 'member'
    session = openingSession()
    answerResult = {
      session: {
        tenant_id: 'demo',
        updated_at: '2026-09-17T10:05:00',
        updated_by: 'alice',
        state: {
          turns: [
            { role: 'assistant', text: '先说说你们主要做什么生意？' },
            { role: 'user', text: '我们主要卖服装和家居' },
            { role: 'assistant', text: '你们有多少个门店？' },
          ],
          skeleton: {
            term_types: [
              {
                value: '商品',
                display_name: '商品',
                rationale: '用户说主要卖服装和家居，商品是最小的售卖单位',
                confidence: 'guess',
                from_turn: 1,
                review: 'pending',
                extra_fields: [],
                standard_name_value_type: 'string',
              },
            ],
            relation_types: [],
            constraints: [],
          },
          questions: [],
          done: false,
        },
      },
      turn: { question: '你们有多少个门店？', added_count: 1, dropped: [], note: null },
    }
    renderSmart()
    const input = await screen.findByLabelText('回答')
    await userEvent.type(input, '我们主要卖服装和家居')
    await userEvent.click(screen.getByRole('button', { name: '回答' }))
    expect(await screen.findByText('商品')).toBeInTheDocument()
    expect(screen.getByText('这是猜的')).toBeInTheDocument()
    expect(screen.getByText('用户说主要卖服装和家居，商品是最小的售卖单位')).toBeInTheDocument()

    const calls = (fetch as unknown as { mock: { calls: [string, RequestInit?][] } }).mock.calls
    const answerCall = calls.find(([url]) => String(url).includes('/interview/answer'))
    expect(answerCall).toBeDefined()
    const body = JSON.parse(String(answerCall![1]?.body))
    expect(body).toEqual({ answer: '我们主要卖服装和家居', updated_at: '2026-09-17T10:00:00' })
  })

  it('这一轮没能认出新概念时，note 显示在对话流末尾', async () => {
    signedInRole = 'member'
    session = openingSession()
    answerResult = {
      session,
      turn: {
        question: '先说说你们主要做什么生意？',
        added_count: 0,
        dropped: [],
        note: '这一轮没能从回答里认出新概念，可以换个说法再说一次',
      },
    }
    renderSmart()
    const input = await screen.findByLabelText('回答')
    await userEvent.type(input, '嗯……不太清楚怎么说')
    await userEvent.click(screen.getByRole('button', { name: '回答' }))
    expect(await screen.findByRole('status')).toHaveTextContent(
      '这一轮没能从回答里认出新概念，可以换个说法再说一次',
    )
  })

  it('只有丢弃、没有 note 时，丢弃理由逐条显示在对话流末尾', async () => {
    // 回归测试：dropped 后端是 string[]，此前前端当 number 用
    // （`dropped > 0`），note 为 null 时整段 <p role="status"> 都不渲染，
    // 用户完全看不到被丢弃的建议。
    signedInRole = 'member'
    session = openingSession()
    answerResult = {
      session,
      turn: {
        question: '下一个问题？',
        added_count: 0,
        dropped: ['实体类型 X 没有给出理由，丢弃'],
        note: null,
      },
    }
    renderSmart()
    const input = await screen.findByLabelText('回答')
    await userEvent.type(input, '随便说点什么')
    await userEvent.click(screen.getByRole('button', { name: '回答' }))
    expect(await screen.findByText('实体类型 X 没有给出理由，丢弃')).toBeInTheDocument()
  })

  it('接受一条候选实体会把整份访谈状态存回去', async () => {
    signedInRole = 'member'
    session = sessionWithTerm('pending')
    renderSmart()
    await userEvent.click(await screen.findByRole('button', { name: '接受 商品' }))
    await waitFor(() => expect(saved).toHaveLength(1))
    expect(saved[0].state.skeleton.term_types[0].review).toBe('accepted')
  })

  it('问题清单：录入问题反推缺的实体，一键加进骨架', async () => {
    signedInRole = 'member'
    session = sessionWithTerm('pending')
    questionResult = {
      session: {
        ...(session as { tenant_id: string; updated_at: string; updated_by: string; state: unknown }),
        state: {
          ...(session as { state: { turns: unknown; skeleton: unknown } }).state,
          questions: [
            {
              text: '哪个品类卖得最好',
              needs: { term_types: ['品类', '商品'], relation_types: [] },
              missing: ['品类'],
              at: '2026-09-17T10:10:00',
            },
          ],
        },
      },
      question: {
        text: '哪个品类卖得最好',
        needs: { term_types: ['品类', '商品'], relation_types: [] },
        missing: ['品类'],
        at: '2026-09-17T10:10:00',
      },
    }
    renderSmart()
    const input = await screen.findByLabelText('业务问题')
    await userEvent.type(input, '哪个品类卖得最好')
    await userEvent.click(screen.getByRole('button', { name: '加一条' }))
    expect(await screen.findByText(/缺：品类/)).toBeInTheDocument()
    const addButton = await screen.findByRole('button', { name: '把 品类 加进骨架' })
    await userEvent.click(addButton)
    await waitFor(() => expect(saved).toHaveLength(1))
    const values = saved[0].state.skeleton.term_types.map((t) => t.value)
    expect(values).toContain('品类')
    expect(values).toContain('商品')
  })

  it('缺的名字全大写也按 needs 里的归属加成实体，不按名字形状猜', async () => {
    // 回归测试：此前用 /^[A-Z][A-Z0-9_]{0,63}$/ 猜 kind，"SKU"这类全大写
    // 的实体名会被误加成关系类型。needs.term_types 才是权威来源。
    signedInRole = 'member'
    session = sessionWithTerm('pending')
    questionResult = {
      session: {
        ...(session as { tenant_id: string; updated_at: string; updated_by: string; state: unknown }),
        state: {
          ...(session as { state: { turns: unknown; skeleton: unknown } }).state,
          questions: [
            {
              text: '每个 SKU 卖了多少',
              needs: { term_types: ['SKU'], relation_types: [] },
              missing: ['SKU'],
              at: '2026-09-17T10:10:00',
            },
          ],
        },
      },
      question: {
        text: '每个 SKU 卖了多少',
        needs: { term_types: ['SKU'], relation_types: [] },
        missing: ['SKU'],
        at: '2026-09-17T10:10:00',
      },
    }
    renderSmart()
    const input = await screen.findByLabelText('业务问题')
    await userEvent.type(input, '每个 SKU 卖了多少')
    await userEvent.click(screen.getByRole('button', { name: '加一条' }))
    const addButton = await screen.findByRole('button', { name: '把 SKU 加进骨架' })
    await userEvent.click(addButton)
    await waitFor(() => expect(saved).toHaveLength(1))
    const skeleton = saved[0].state.skeleton as unknown as {
      term_types: { value: string }[]
      relation_types: { relation_type: string }[]
    }
    expect(skeleton.term_types.map((t) => t.value)).toContain('SKU')
    expect(skeleton.relation_types).toHaveLength(0)
  })

  it('写入草稿：先看差异、删除项弹确认框，确认后走 draft/replace 且不带 ETL 映射', async () => {
    signedInRole = 'member'
    session = sessionWithTerm('accepted')
    applyPreviewRemovedTermTypes = ['手工加的']
    renderSmart()
    await userEvent.click(await screen.findByRole('button', { name: '写入草稿' }))
    expect(await screen.findByText(/会删掉：手工加的/)).toBeInTheDocument()
    await userEvent.click(await screen.findByRole('button', { name: '继续写入' }))
    await waitFor(() => expect(draftReplaceBodies).toHaveLength(1))
    const body = draftReplaceBodies[0] as { term_types: { value: string }[]; etl_mapping: unknown }
    expect(body.term_types[0].value).toBe('商品')
    expect(body.etl_mapping).toBeNull()
  })

  it('确认框里点取消，不写入草稿', async () => {
    signedInRole = 'member'
    session = sessionWithTerm('accepted')
    applyPreviewRemovedTermTypes = ['手工加的']
    renderSmart()
    await userEvent.click(await screen.findByRole('button', { name: '写入草稿' }))
    await userEvent.click(await screen.findByRole('button', { name: '取消' }))
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(draftReplaceBodies).toHaveLength(0)
  })
})
