import { beforeEach, describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { ADMIN_ROUTES } from '../adminRoutes'
import type { EtlMapping } from './etlMappingApi'

/**
 * 表格导入页首屏：本体是否已经带着引导配好的映射，决定用户看到的是
 * 「传数据文件即可」还是「从头配置」。这是 admin-flow-continuity 计划的
 * 第三个任务——前两个任务已经让映射与本体同生命周期存储、并提供了
 * fetchEtlMapping 读取接口，本任务是第一个消费方。
 */

let etlMappingStubbed = false
let etlMappingValue: EtlMapping | null = null

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
      if (url.includes('/schema-etl/status')) {
        return json({ ontology_confirmed: true })
      }
      if (url.includes('/schema-etl/runs')) {
        return json({ runs: [] })
      }
      if (url.includes('/schema-etl/sample')) {
        return json({ files: [] })
      }
      // /etl-mapping 只有测试显式调用 stubEtlMapping 之后才会 resolve——
      // 没调用就落进下面的 catch-all，永不 resolve，用来模拟「未知态」。
      if (etlMappingStubbed && url.includes('/etl-mapping')) {
        return json({ mapping: etlMappingValue })
      }
      return new Promise(() => {})
    }),
  )
}

function stubEtlMapping(mapping: EtlMapping | null) {
  etlMappingStubbed = true
  etlMappingValue = mapping
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
  etlMappingStubbed = false
  etlMappingValue = null
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

describe('表格导入页首屏', () => {
  it('本体带着引导配好的映射时，只要数据文件，不邀请他重配', async () => {
    // 刚走完引导的用户看到一个邀请他从头配置的界面，等于让他重做刚做完的
    // 工作；重做出来的两份还可能不一致，那时以哪个为准没有答案。
    signIn('admin')
    stubEtlMapping({
      config_yaml: 'entities: []',
      source_file_name: 'orders.csv',
      created_at: '2026-09-03T00:00:00',
    })
    renderAt(ADMIN_ROUTES.etl)
    expect(await screen.findByText(/引导流程已为这个本体配好映射/)).toBeTruthy()
    expect(screen.getByText('orders.csv')).toBeTruthy()
    // 构建器降级成折叠的次级入口，不是主角。
    expect(screen.getByRole('button', { name: /改这份映射／再接一张表/ })).toBeTruthy()
  })

  it('没有映射时维持原样，构建器是主角', async () => {
    signIn('admin')
    stubEtlMapping(null)
    renderAt(ADMIN_ROUTES.etl)
    expect(await screen.findByRole('button', { name: /把这张表映射到已有本体/ })).toBeTruthy()
    expect(screen.queryByText(/引导流程已为这个本体配好映射/)).toBeNull()
  })

  it('映射状态未知时，不抢先渲染任何一种形态', async () => {
    // 抢先渲染"从头配置"会让刚走完引导的用户看到一个邀请他重做的界面，
    // 然后闪一下变掉。未知就是未知，不许折叠进任何一个已知态。
    signIn('admin')
    renderAt(ADMIN_ROUTES.etl)
    expect(await screen.findByTestId('etl-mapping-loading')).toBeTruthy()
    expect(screen.queryByText(/引导流程已为这个本体配好映射/)).toBeNull()
    expect(screen.queryByRole('button', { name: /把这张表映射到已有本体/ })).toBeNull()
  })
})
