import { beforeEach, describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import App from '../../App'
import { SkinProvider } from '../SkinContext'
import { ConfirmProvider } from '../ConfirmContext'
import { ToastProvider } from '../ToastContext'
import { ADMIN_ROUTES } from '../../adminRoutes'
import type { TermType } from '../ontologyTypes'
import { resetAdminSession } from '../useAdminAuth'

/**
 * 身份不再存 sessionStorage（token 在 HttpOnly Cookie 里，JS 读不到，也
 * 塞不进去）：界面从 whoami 拿身份，所以这里要打桩的是 whoami。
 */
let signedInRole: 'admin' | 'member' | null = null

function whoamiResponse() {
  if (signedInRole === null) {
    return Promise.resolve(new Response(JSON.stringify({ detail: '未登录' }), { status: 401 }))
  }
  return Promise.resolve(
    new Response(
      JSON.stringify({
        username: signedInRole === 'admin' ? 'admin' : 'alice',
        role: signedInRole,
        tenant_id: signedInRole === 'admin' ? null : 'demo',
        current_tenant_id: 'demo',
      }),
      { status: 200 },
    ),
  )
}

function signIn(role: 'admin' | 'member') {
  signedInRole = role
}

/**
 * 本体结构页的引导入口。
 *
 * 引导负责从零到一，本体结构页那三个 tab 负责后续微调，两条路径都留着。
 * replace_draft 是整份替换：草稿非空时不提示，用户走完引导回来会发现
 * 手工建的东西没了，而且不知道是这一步干的——所以入口的 title 要按草稿
 * 是否有内容分别措辞。
 */

function stubOntology({ termTypes }: { termTypes: TermType[] } = { termTypes: [] }) {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/auth/whoami')) return whoamiResponse()
      const json = (body: unknown) =>
        Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))
      if (url.includes('/ontology/') && url.includes('/status')) return json({ confirmed: false })
      if (url.includes('/checkout')) return json({})
      if (url.includes('/term-types')) return json({ term_types: termTypes })
      if (url.includes('/relation-types')) return json({ relation_types: [] })
      if (url.includes('/constraints')) return json({ constraints: [] })
      if (url.includes('/api/admin/tenants')) {
        return json({ tenants: [{ tenant_id: 'demo', name: '演示租户', status: 'active' }] })
      }
      return new Promise(() => {})
    }),
  )
}

/**
 * checkout/term-types 永不 resolve——把 readiness 钉在初始的 null 上。
 *
 * 真实网络下 null 只存在极短一瞬（首帧到 checkout+三个 draft 查询回来
 * 之间），用会 resolve 的 mock 去抓这一瞬是在赌微任务调度顺序，不可靠。
 * 钉住它才能确定地断言"未知态"的文案，而不是刚好在 fetch resolve 前
 * 抢到的巧合。
 */
function stubOntologyPending() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/auth/whoami')) return whoamiResponse()
      const json = (body: unknown) =>
        Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))
      if (url.includes('/api/admin/tenants')) {
        return json({ tenants: [{ tenant_id: 'demo', name: '演示租户', status: 'active' }] })
      }
      // /status、/checkout、/term-types 等 ontology 相关请求全部悬空，
      // 模拟"还没查完"的状态。
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  signedInRole = null
  resetAdminSession()
  sessionStorage.clear()
  localStorage.clear()
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

describe('引导入口', () => {
  it('本体结构页有引导入口', async () => {
    signIn('admin')
    stubOntology()
    renderAt(ADMIN_ROUTES.ontology)
    expect(await screen.findByRole('link', { name: /引导|从表格开始/ })).toBeTruthy()
  })

  it('还不知道草稿是否为空时，既不承诺安全也不警告覆盖', async () => {
    // readiness 初始是 null——这一刻还没查到草稿是否为空。此时就说
    // "安全"或"会覆盖"都是在不知情的情况下许诺，一旦猜错，用户要么白
    // 白被吓退，要么手工建的东西被覆盖了还不知道是这一步干的。
    signIn('admin')
    stubOntologyPending()
    renderAt(ADMIN_ROUTES.ontology)
    const link = await screen.findByRole('link', { name: /引导|从表格开始/ })
    const title = link.getAttribute('title') ?? ''
    expect(title).not.toMatch(/覆盖|替换/)
    expect(title).not.toMatch(/推荐一套/)
  })

  it('已经有草稿时，入口要提示会被覆盖', async () => {
    // replace_draft 是整份替换。不提示的话，用户点进引导、走完流程，
    // 手工建的那些东西没了，而他不知道是这一步干的。
    signIn('admin')
    stubOntology({ termTypes: [{ value: '已有类型', extra_fields: [], standard_name_value_type: 'string' }] })
    renderAt(ADMIN_ROUTES.ontology)
    const link = await screen.findByRole('link', { name: /引导|从表格开始/ })
    // 等 readiness 真的拉取完成、title 定型成"已知非空"那句之后再断言，
    // 否则会和上一条"未知"态断言撞在同一句"不含覆盖"上，测不出区别。
    await screen.findByTitle(/覆盖|替换/)
    expect(link.getAttribute('title')).toMatch(/覆盖|替换/)
  })

  it('草稿为空时不提示覆盖——没有东西可覆盖', async () => {
    signIn('admin')
    stubOntology({ termTypes: [] })
    renderAt(ADMIN_ROUTES.ontology)
    const link = await screen.findByRole('link', { name: /引导|从表格开始/ })
    // "未知"和"已知为空"两态的 title 都不含"覆盖"，容易被同一条断言
    // 误判成一条测试。这里等 title 变成"已知为空"那句具体文案定型后
    // 再断言，确保测的是"已知为空"而不是首帧还没查完的"未知"。
    await screen.findByTitle(/推荐一套|从一张业务表开始，/)
    expect(link.getAttribute('title') ?? '').not.toMatch(/覆盖/)
  })
})
