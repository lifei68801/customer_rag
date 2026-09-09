import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { ADMIN_ROUTES } from '../adminRoutes'
import { resetAdminSession } from './useAdminAuth'

/**
 * 属性值冲突审核页。
 *
 * 这一页的价值全在「审核员凭什么做这个决定」：两个值 + 各自来源。只给两个
 * 数字的话，他没有任何依据判断该信哪个。
 */
interface ConflictRow {
  conflict_id: number
  node_key: string
  field: string
  kept_value: string
  kept_source: string
  incoming_value: string
  incoming_source: string
}

const CONFLICT: ConflictRow = {
  conflict_id: 7,
  node_key: '产品:洗发水',
  field: 'price',
  kept_value: '39',
  kept_source: '商品表.xlsx',
  incoming_value: '45',
  incoming_source: '价格库.csv',
}

let conflicts: ConflictRow[]
let listStatus = 200
let resolveStatus = 200
let resolveDetail = ''
let resolveBodies: unknown[] = []

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
              current_tenant_id: 'demo',
            }),
            { status: 200 },
          ),
        )
      }
      if (url.includes('/resolve')) {
        resolveBodies.push(JSON.parse(String(init?.body ?? '{}')))
        if (resolveStatus !== 200) {
          return Promise.resolve(
            new Response(JSON.stringify({ detail: resolveDetail }), { status: resolveStatus }),
          )
        }
        return Promise.resolve(new Response(JSON.stringify({ resolved_value: 'x' }), { status: 200 }))
      }
      if (url.includes('/conflicts')) {
        return Promise.resolve(
          new Response(
            JSON.stringify(listStatus === 200 ? { conflicts, total: conflicts.length } : {}),
            { status: listStatus },
          ),
        )
      }
      if (url.includes('/nav-badges')) {
        return Promise.resolve(new Response(JSON.stringify({}), { status: 200 }))
      }
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  conflicts = [CONFLICT]
  listStatus = 200
  resolveStatus = 200
  resolveDetail = ''
  resolveBodies = []
  resetAdminSession()
  localStorage.clear()
  stubApi()
})

async function renderPage() {
  const result = render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[ADMIN_ROUTES.reviewConflicts]}>
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
  await screen.findByTestId('admin-topbar')
  return result
}

const card = () => within(screen.getByTestId('conflict-7'))

describe('属性冲突审核页', () => {
  it('一条冲突把两个值和各自来源都摆出来', async () => {
    // 只给两个数字的话，审核员没有任何依据判断该信哪个——「哪张表更权威」
    // 往往就是他做这个决定的全部依据。
    await renderPage()

    await waitFor(() => expect(screen.getByTestId('conflict-7')).toBeTruthy())
    expect(card().getByRole('button', { name: /39.*商品表\.xlsx/ })).toBeTruthy()
    expect(card().getByRole('button', { name: /45.*价格库\.csv/ })).toBeTruthy()
    // 哪个实体的哪个属性也要说清——不说的话审核员在给一个匿名的数字投票。
    expect(card().getByText(/产品:洗发水.*price/)).toBeTruthy()
  })

  it('三个动作都在：选 A、选 B、手填', async () => {
    // 两个来源都错是可能的（比如两张表都漏了单位），强制二选一等于逼他
    // 选一个已知是错的。
    const user = userEvent.setup()
    await renderPage()
    await waitFor(() => expect(screen.getByTestId('conflict-7')).toBeTruthy())

    await user.type(card().getByRole('textbox', { name: /手填/ }), '39.00 元')
    await user.click(card().getByRole('button', { name: '用这个值' }))

    await waitFor(() => expect(resolveBodies).toEqual([{ value: '39.00 元' }]))
  })

  it('选哪个按钮就把哪个值发出去', async () => {
    // 两个按钮接到同一个值上的话，界面看起来完全正常，而审核员选 45 得到
    // 的是 39——他不会回头核对自己刚点过的东西。
    const user = userEvent.setup()
    await renderPage()
    await waitFor(() => expect(screen.getByTestId('conflict-7')).toBeTruthy())

    await user.click(card().getByRole('button', { name: /45/ }))

    await waitFor(() => expect(resolveBodies).toEqual([{ value: '45' }]))
  })

  it('决议之后这条从列表里消失', async () => {
    const user = userEvent.setup()
    await renderPage()
    await waitFor(() => expect(screen.getByTestId('conflict-7')).toBeTruthy())

    await user.click(card().getByRole('button', { name: /39/ }))

    await waitFor(() => expect(screen.queryByTestId('conflict-7')).toBeNull())
  })

  it('决议失败时这条留在原地，并把后端说的原话显示出来', async () => {
    // 消失的话审核员以为处理完了。而后端的 503 会说「值已写进术语表，但
    // 同步图谱失败」——那句话说清了这次操作做了一半，包装成「操作失败」
    // 等于让他以为什么都没发生。
    resolveStatus = 503
    resolveDetail = '值已写进术语表，但同步图谱失败。这条冲突仍在队列里，请稍后重试。'
    const user = userEvent.setup()
    await renderPage()
    await waitFor(() => expect(screen.getByTestId('conflict-7')).toBeTruthy())

    await user.click(card().getByRole('button', { name: /39/ }))

    await waitFor(() => expect(card().getByText(/同步图谱失败/)).toBeTruthy())
    expect(screen.getByTestId('conflict-7')).toBeTruthy()
  })

  it('一条都没有时说清是真的没有，不是还没加载', async () => {
    conflicts = []
    await renderPage()
    await waitFor(() => expect(screen.getByText(/没有待处理的属性冲突/)).toBeTruthy())
  })

  it('列表拉不到时说出来，不是显示成「一条冲突都没有」', async () => {
    // 「没有冲突」会让审核员安心走开，而真相是这一页根本没查成。
    listStatus = 500
    await renderPage()
    await waitFor(() => expect(screen.getByText(/冲突列表加载失败/)).toBeTruthy())
    expect(screen.queryByText(/没有待处理的属性冲突/)).toBeNull()
  })
})
