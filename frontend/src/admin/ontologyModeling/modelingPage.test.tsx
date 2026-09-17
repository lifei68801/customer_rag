import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import App from '../../App'
import { SkinProvider } from '../SkinContext'
import { ConfirmProvider } from '../ConfirmContext'
import { ToastProvider } from '../ToastContext'
import { ADMIN_ROUTES, modelingWay } from '../../adminRoutes'
import { resetAdminSession } from '../useAdminAuth'

/**
 * 本体建模页：本体结构与建模工作台并成一页的三个 tab（模板 / 手动 / 智能）。
 *
 * 这里测的是壳——tab 与 URL 的同步、旧地址的落点、侧边栏只剩一项；三个
 * tab 各自的内容由它们原来的测试盖着（ontologyConfirm、workbenchPage 等）。
 */

function whoami() {
  return Promise.resolve(
    new Response(
      JSON.stringify({ username: 'alice', role: 'member', tenant_id: 'demo', current_tenant_id: 'demo' }),
      { status: 200 },
    ),
  )
}

const json = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status }))

beforeEach(() => {
  resetAdminSession()
  sessionStorage.clear()
  localStorage.clear()
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/auth/whoami')) return whoami()
      if (url.includes('/nav-badges')) return json({ pending_relations: 0, pending_duplicates: 0, total_terms: 0 })
      if (url.includes('/ontology/demo/status')) return json({ confirmed: false })
      if (url.includes('/draft/checkout')) return json({ ok: true })
      if (url.includes('/term-types')) return json({ term_types: [] })
      if (url.includes('/relation-types')) return json({ relation_types: [] })
      if (url.includes('/constraints')) return json({ constraints: [] })
      if (url.includes('/modeling-workspace/skills')) return json({ skills: [] })
      if (url.includes('/modeling-workspace/grounding'))
        return json({ status: null, grounded_term_types: [], grounded_relation_types: [], source_files: [], parse_error: null })
      if (url.includes('/modeling-workspace')) return json({ workspace: null })
      if (url.includes('/interview')) return json({ session: null })
      return new Promise(() => {})
    }),
  )
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

const tabs = () => within(screen.getByRole('group', { name: '构建方式' }))

describe('本体建模页', () => {
  it('侧边栏本体创建下第一项是本体建模，旧的两项没了', async () => {
    renderAt(ADMIN_ROUTES.ontologyModeling)
    const nav = within(await screen.findByRole('navigation', { name: '后台导航' }))
    const labels = nav.getAllByRole('link').map((a) => a.textContent?.trim())
    expect(labels.indexOf('本体建模')).toBeLessThan(labels.indexOf('本体图'))
    expect(labels).not.toContain('本体结构')
    expect(labels).not.toContain('建模工作台')
  })

  it('缺省落在手动构建——多数租户在维护已有本体，不该被扔进空工作区', async () => {
    renderAt(ADMIN_ROUTES.ontologyModeling)
    expect(await screen.findByRole('heading', { name: '本体建模' })).toBeInTheDocument()
    expect(tabs().getByRole('button', { name: '手动构建' }).getAttribute('aria-pressed')).toBe('true')
    // 手动构建就是原来的本体结构：三个子 tab 还在
    expect(await screen.findByRole('button', { name: '实体类型' })).toBeInTheDocument()
  })

  it('切到模板构建改 URL 并渲染工作台', async () => {
    renderAt(ADMIN_ROUTES.ontologyModeling)
    await userEvent.click(await screen.findByRole('button', { name: '模板构建' }))
    expect(screen.getByTestId('url').textContent).toBe(modelingWay('template'))
    // 用按钮而不是文本：空白起步这四个字在这一屏出现多次（标题、说明、按钮）
    expect(await screen.findByRole('button', { name: '空白起步' })).toBeInTheDocument()
  })

  it('切 tab 保留别的查询参数（version 等）', async () => {
    renderAt(`${modelingWay('manual')}&version=confirmed`)
    await userEvent.click(await screen.findByRole('button', { name: '模板构建' }))
    expect(screen.getByTestId('url').textContent).toContain('version=confirmed')
    expect(screen.getByTestId('url').textContent).toContain('way=template')
  })

  it('版本切换器只在手动构建里', async () => {
    renderAt(modelingWay('manual'))
    expect(await screen.findByRole('group', { name: '本体版本' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '模板构建' }))
    expect(screen.queryByRole('group', { name: '本体版本' })).toBeNull()
  })

  it('旧地址重定向到对应的 tab', async () => {
    renderAt('/admin/ontology/ontology')
    await screen.findByRole('heading', { name: '本体建模' })
    expect(screen.getByTestId('url').textContent).toBe(modelingWay('manual'))
  })

  it('旧的引导建模地址重定向到模板构建', async () => {
    renderAt('/admin/ontology/guided')
    await screen.findByRole('heading', { name: '本体建模' })
    expect(screen.getByTestId('url').textContent).toBe(modelingWay('template'))
  })
})
