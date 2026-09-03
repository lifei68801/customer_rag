import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { PAGE_TITLES } from '../adminRoutes'

/**
 * 后台落地路由:/admin 该分流到哪一步,取决于本体是否已确认。
 *
 * 侧边栏顺序表达的是依赖(建模 → 接入 → 审核):ETL 会拒绝未确认本体的
 * 租户,文档管线在本体未确认时会跳过图谱抽取。静态跳文档上传等于教
 * 新租户走一条产品会拒绝的路;静态跳本体结构又会让天天来传文档的老
 * 租户每次被丢回建模页。状态未知时——请求还没回来——两边都不能跳,
 * 也不能空白,因为跳任何一边都是对用户断言一件可能为假的事。
 */

let ontologyStatusResponse: { confirmed: boolean } | null = null

function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      const json = (body: unknown, status = 200) =>
        Promise.resolve(new Response(JSON.stringify(body), { status }))
      if (url.includes('/nav-badges')) {
        return json({ pending_relations: 0, pending_duplicates: 0, total_terms: 0 })
      }
      if (url.includes('/ontology/demo/status') && ontologyStatusResponse !== null) {
        return json(ontologyStatusResponse)
      }
      // 未匹配的 URL(以及故意不 stub 状态接口的情形)永不 resolve——这就是
      // 「状态未知」的天然造法,不需要另写一个 never-resolve 辅助函数。
      return new Promise(() => {})
    }),
  )
}

function stubOntologyStatus(body: { confirmed: boolean }) {
  ontologyStatusResponse = body
}

function signIn(role: 'admin' | 'member') {
  sessionStorage.setItem('admin_session_token', 'tok')
  sessionStorage.setItem('admin_username', role === 'admin' ? 'admin' : 'alice')
  sessionStorage.setItem('admin_role', role)
  sessionStorage.setItem('admin_current_tenant', 'demo')
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  ontologyStatusResponse = null
  stubApi()
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

describe('后台落地路由', () => {
  it('本体没确认时落在本体结构页', async () => {
    // 侧边栏顺序表达的是依赖:ETL 会拒绝未确认本体的租户,文档管线会跳过
    // 图谱抽取。落地在第二阶段等于教用户走一条产品会拒绝的路。
    signIn('admin')
    stubOntologyStatus({ confirmed: false })
    renderAt('/admin')
    // 侧边栏的「本体结构」链接和页面标题文字相同,用 heading 角色定位到
    // 页面本体的 <h1>,不是 findByText——后者在两处都命中会直接报错。
    expect(await screen.findByRole('heading', { name: PAGE_TITLES.ontology })).toBeTruthy()
  })

  it('本体已确认时落在文档上传', async () => {
    // 老租户天天来传文档,不该每次被丢回建模页。
    signIn('admin')
    stubOntologyStatus({ confirmed: true })
    renderAt('/admin')
    expect(await screen.findByRole('heading', { name: PAGE_TITLES.documents })).toBeTruthy()
  })

  it('状态未知时不跳转、也不空白', async () => {
    signIn('admin')
    // 故意不 stub 状态接口:fetch 落进 stubApi() 的兜底分支,永不 resolve,
    // 天然构成「状态未知」。
    renderAt('/admin')
    expect(await screen.findByTestId('admin-landing-loading')).toBeTruthy()
    expect(screen.queryByRole('heading', { name: PAGE_TITLES.documents })).toBeNull()
    expect(screen.queryByRole('heading', { name: PAGE_TITLES.ontology })).toBeNull()
  })
})
