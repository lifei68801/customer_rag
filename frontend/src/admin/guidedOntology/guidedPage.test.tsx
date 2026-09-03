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
import { MAX_XLSX_BYTES } from './columnStats'

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

const PRODUCTS = ['咖啡', '茶', '可乐']

/**
 * 纯数字订单号的样表。列数固定 3 列（下面有用例断言"共 3 列"）。
 *
 * 25 行不是随便取的：columnStats 的 NUMERIC_IDENTIFIER_THRESHOLD(=50) 之下
 * 的纯整数列会保持 inferredType 'integer'，而 columnRoles 的
 * INTEGER_IDENTIFIER_MIN_ROWS(=20) 要求至少 20 个非空值才肯把整数列判成
 * 标识。25 行正好落在这两道门中间——它是"数字订单号能被认出来"这条链路
 * 唯一会走到的区间，也是这份 fixture 存在的理由。
 * 产品只有 3 个取值（比例 0.12 ≤ DIMENSION_MAX_RATIO），才会被判成维度；
 * revenue 带小数，保持 'number' 走度量。
 */
const csvFile = () => {
  const rows = Array.from(
    { length: 25 },
    (_, i) => `${1001 + i},${PRODUCTS[i % 3]},${10.5 + i}\n`,
  ).join('')
  return new File([`订单号,产品,revenue\n${rows}`], 'orders.csv', { type: 'text/csv' })
}

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

  it('纯数字的订单号走完真实扫描后，是本体的中心实体', async () => {
    // 唯一一条从 File 到 UI 的真实链路。缺了它，columnStats 的
    // NUMERIC_IDENTIFIER_THRESHOLD 与 columnRoles 的 classify 之间的接缝
    // 在集成层没有守卫：两边各自的单测都绿，而整数订单号在真实页面上被
    // 判成度量、连带整个实体消失——这个 Critical 缺陷正是这么漏出去的。
    signIn('admin')
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.guidedOntology)
    await user.upload(await screen.findByLabelText(/选择一张表/), csvFile())

    // 产品（维度）挂在订单号下面：这同时证明订单号进了实体，而且是中心。
    const select = (await screen.findByLabelText(/产品 挂在/)) as HTMLSelectElement
    expect(select.value).toBe('订单号')
    // 判成度量的话它会静静变成一个属性；判成自由文本的话会落进未使用清单。
    const unused = await screen.findByTestId('unused-columns')
    expect(unused.textContent).not.toMatch(/订单号/)
  })

  it('复核时能换一张表，不用刷新页面', async () => {
    // 审阅视图的「没有用到的列」一节写着"用上面的「换一张表」重传一张更
    // 聚焦的表"。review 步骤此前没有任何回退控件（文件输入框只在
    // step === 'upload' 时渲染），那句话是一个做不到的承诺，用户只能刷新
    // 页面重来、连带丢掉全部改判。
    signIn('admin')
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.guidedOntology)
    await user.upload(await screen.findByLabelText(/选择一张表/), csvFile())
    await screen.findByText(/扫描完成，共 3 列/)

    await user.click(screen.getByRole('button', { name: '换一张表' }))
    // 回到第一步：文件输入框在，上一张表的审阅视图不在。
    expect(await screen.findByLabelText(/选择一张表/)).toBeTruthy()
    expect(screen.queryByText(/扫描完成/)).toBeNull()
    expect(screen.queryByTestId('unused-columns')).toBeNull()
  })

  it('改判过之后换表要先确认，取消就留在原地', async () => {
    // 换表清空 roled/decision/uploadedFile，不可撤销。改判是用户在这个页面
    // 上唯一真正花了力气的东西，一键丢掉而不问，等于把「手滑」和「我确实
    // 想重来」当成同一件事。本项目其余丢弃工作的入口（AccountsPage /
    // DocumentsPage / TermsPage / OntologySchemaPage）一律走 useConfirm。
    signIn('admin')
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.guidedOntology)
    await user.upload(await screen.findByLabelText(/选择一张表/), csvFile())
    await screen.findByText(/扫描完成，共 3 列/)

    // 把维度列「产品」改判成属性——这就是不该被静默丢掉的那份工作。
    const asAttribute = screen.getAllByRole('radio', { name: /做成属性/ })
    await user.click(asAttribute[asAttribute.length - 1])

    await user.click(screen.getByRole('button', { name: '换一张表' }))
    // 取消：什么都没丢，仍在审阅视图。
    await user.click(await screen.findByRole('button', { name: '取消' }))
    expect(await screen.findByText(/扫描完成，共 3 列/)).toBeTruthy()
    expect(screen.queryByLabelText(/选择一张表/)).toBeNull()

    // 确认：这才回到第一步。
    await user.click(screen.getByRole('button', { name: '换一张表' }))
    await user.click(await screen.findByRole('button', { name: '换表' }))
    expect(await screen.findByLabelText(/选择一张表/)).toBeTruthy()
    expect(screen.queryByText(/扫描完成/)).toBeNull()
  })

  it('扫描失败时说清原因，不是静静停住', async () => {
    signIn('admin')
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.guidedOntology)
    await user.upload(await screen.findByLabelText(/选择一张表/), oversizedXlsx())
    const alert = await screen.findByRole('alert')
    // 只断言 alert 存在测不出任何东西：实测过把 setError(err.message) 换成
    // 常量 '出错了' 之后全部测试依旧全绿，而这条测试的名字承诺的是"说清
    // 原因"。要断言的是原因本身——是哪个限制、超了多少。
    expect(alert.textContent).toMatch(/xlsx/)
    expect(alert.textContent).toMatch(/上限/)
    expect(alert.textContent).toMatch(new RegExp(String(MAX_XLSX_BYTES)))
  })

  it('member 看到的是无权限提示，不是 404', async () => {
    signIn('member')
    renderAt(ADMIN_ROUTES.guidedOntology)
    expect(await screen.findByTestId('no-permission')).toBeTruthy()
  })
})
