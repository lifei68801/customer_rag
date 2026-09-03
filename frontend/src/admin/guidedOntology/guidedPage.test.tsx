import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../../App'
import { SkinProvider } from '../SkinContext'
import { ConfirmProvider } from '../ConfirmContext'
import { ToastProvider } from '../ToastContext'
import { ADMIN_ROUTES } from '../../adminRoutes'
import * as columnStats from './columnStats'

// 默认透传真实实现：只有「扫描中显示进度」这一条测试需要手动控制这个
// mock 何时 resolve，其余测试（尤其是 oversizedXlsx 那条，验证的就是真实
// 扫描函数抛出的错误消息）必须走真实的 scanTableFile。
vi.mock('./columnStats', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./columnStats')>()
  return { ...actual, scanTableFile: vi.fn(actual.scanTableFile) }
})

/**
 * 引导页第一步：传一张表并扫描。
 *
 * 扫描一张大表要几秒——什么都不显示的话用户会以为页面卡了，然后重复
 * 点击或刷新。失败也要说清原因，不能静静停住。
 */

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
      return new Promise(() => {})
    }),
  )
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

const csvFile = () =>
  new File(
    ['订单号,产品,revenue\n1001,咖啡,10.5\n1002,茶,20\n1003,咖啡,30\n'],
    'orders.csv',
    { type: 'text/csv' },
  )

const oversizedXlsx = () =>
  new File([new Uint8Array(21 * 1024 * 1024)], 'big.xlsx')

describe('引导页第一步', () => {
  it('一开始只要求传一张表', async () => {
    signIn('admin')
    renderAt(ADMIN_ROUTES.guidedOntology)
    expect(await screen.findByLabelText(/选择一张表/)).toBeTruthy()
  })

  it('扫描中显示进度，不是空白', async () => {
    // 扫描一张大表要几秒。什么都不显示的话用户会以为页面卡了，然后重复
    // 点击或刷新。这里用一个手动控制的 deferred promise 卡住
    // scanTableFile，断言依赖它的 pending/resolved 状态，不依赖 wall-clock。
    signIn('admin')
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.guidedOntology)

    let resolveScan: (stats: Awaited<ReturnType<typeof columnStats.scanTableFile>>) => void = () => {}
    const deferred = new Promise<Awaited<ReturnType<typeof columnStats.scanTableFile>>>((resolve) => {
      resolveScan = resolve
    })
    vi.mocked(columnStats.scanTableFile).mockReturnValueOnce(deferred)

    await user.upload(await screen.findByLabelText(/选择一张表/), csvFile())
    expect(await screen.findByText(/正在扫描/)).toBeTruthy()

    resolveScan([])
    expect(await screen.findByText(/扫描完成/)).toBeTruthy()
  })

  it('扫描成功后进入复核，显示扫描出的列数', async () => {
    // 不打桩：走真实的 scanTableFile，守住成功路径的接线。
    signIn('admin')
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.guidedOntology)
    await user.upload(await screen.findByLabelText(/选择一张表/), csvFile())
    expect(await screen.findByText(/扫描完成，共 3 列/)).toBeTruthy()
  })

  it('扫描失败时说清原因，不是静静停住', async () => {
    signIn('admin')
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.guidedOntology)
    await user.upload(await screen.findByLabelText(/选择一张表/), oversizedXlsx())
    expect(await screen.findByRole('alert')).toBeTruthy()
  })

  it('member 看到的是无权限提示，不是 404', async () => {
    signIn('member')
    renderAt(ADMIN_ROUTES.guidedOntology)
    expect(await screen.findByTestId('no-permission')).toBeTruthy()
  })
})
