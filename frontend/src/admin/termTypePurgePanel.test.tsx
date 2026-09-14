import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ConfirmProvider } from './ConfirmContext'
import { SkinProvider } from './SkinContext'
import { TermTypePurgePanel } from './TermTypePurgePanel'

/**
 * 彻底清空实体类型。不可逆，所以这组测试钉的是那道闸和它的代价说明；
 * 同时钉住"闸只有一道"——第一版是每个类型各走一遍预览＋手打类型名，
 * demo 的 12 个类型要做 12 遍，用户到第 8 遍就不会再读弹窗了。
 */

type Call = { url: string; body: Record<string, unknown> | null }
let calls: Call[] = []
let storedTypes: { term_type: string; stored: number; visible: number }[] = []
let purgeFailures: Record<string, { status: number; detail: string }> = {}

function json(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status }))
}

beforeEach(() => {
  calls = []
  storedTypes = [
    { term_type: 'Order ID', stored: 10000, visible: 0 },
    { term_type: '订单号', stored: 10000, visible: 0 },
    { term_type: 'Company', stored: 3, visible: 3 },
  ]
  purgeFailures = {}
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const body = init?.body ? JSON.parse(String(init.body)) : null
      calls.push({ url, body })
      if (url.endsWith('/terms/stored-types')) return json({ types: storedTypes })
      if (url.endsWith('/terms/purge/preview')) {
        // 故意比列表里那个数多 1：列表可能是几分钟前拉的，而数量守卫的意义
        // 正是"跟我刚才看到的一致"。两个数相同的话，用哪个都能让测试变绿。
        const row = storedTypes.find((t) => t.term_type === body!.term_type)
        const fresh = (row?.stored ?? 0) + 1
        return json({
          term_type: body!.term_type,
          node_count: fresh,
          stored_rows: fresh,
          created_only: 0,
        })
      }
      if (url.endsWith('/terms/purge')) {
        const failure = purgeFailures[String(body!.term_type)]
        if (failure) return json({ detail: failure.detail }, failure.status)
        storedTypes = storedTypes.filter((t) => t.term_type !== body!.term_type)
        return json({ node_count: body!.expected_node_count, removed_by_table: {} })
      }
      return new Promise(() => {})
    }),
  )
})

function renderPanel(onPurged = vi.fn()) {
  render(
    <SkinProvider>
      <ConfirmProvider>
        <TermTypePurgePanel sessionToken="s" tenantId="demo" onPurged={onPurged} />
      </ConfirmProvider>
    </SkinProvider>,
  )
  return onPurged
}

async function expand(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: /彻底清空实体类型/ }))
  await screen.findByTestId('purge-select-all')
}

async function confirmDialog(user: ReturnType<typeof userEvent.setup>) {
  const dialog = await screen.findByRole('alertdialog')
  await user.click(within(dialog).getByRole('button', { name: '彻底清空' }))
}

describe('彻底清空实体类型', () => {
  it('默认折叠，不展开不发请求', () => {
    renderPanel()

    expect(screen.queryByTestId('purge-select-all')).toBeNull()
    expect(calls).toEqual([])
  })

  it('列出全被删除、在分组摘要里看不见的类型，并说出重新导入不会恢复', async () => {
    const user = userEvent.setup()
    renderPanel()

    await expand(user)

    const row = screen.getByText('Order ID').closest('tr')!
    expect(within(row).getByText(/10,000 条已删除，重新导入不会恢复/)).toBeTruthy()
  })

  it('全选一次、确认一次，就把所有类型清完——不是每个类型各走一遍', async () => {
    const user = userEvent.setup()
    const onPurged = renderPanel()
    await expand(user)

    await user.click(screen.getByTestId('purge-select-all'))
    // 按钮上先说清代价：几个类型、多少个实体。
    const button = screen.getByTestId('purge-selected')
    expect(button.textContent).toMatch(/3 个类型/)
    expect(button.textContent).toMatch(/20,003 个实体/)

    await user.click(button)
    await confirmDialog(user)

    await waitFor(() => expect(onPurged).toHaveBeenCalled())
    const purged = calls.filter((c) => c.url.endsWith('/terms/purge'))
    expect(purged.map((c) => c.body!.term_type)).toEqual(['Order ID', '订单号', 'Company'])
    // 数量守卫用的是刚取的预览值，不是列表里那个可能几分钟前拉的数。
    expect(purged.map((c) => c.body!.expected_node_count)).toEqual([10001, 10001, 4])
    expect(onPurged).toHaveBeenCalledWith(expect.stringContaining('20,006'))
    // 清完刷新列表，三行都没了。按行上的复选框判，不按文字——下面的结果
    // 明细里同样写着类型名。
    await waitFor(() => expect(screen.queryByTestId('purge-select-Order ID')).toBeNull())
    expect(screen.getByText(/存储里没有任何实体/)).toBeTruthy()
  })

  it('确认框里写清删什么、不删什么、不可撤销', async () => {
    const user = userEvent.setup()
    renderPanel()
    await expand(user)
    await user.click(screen.getByTestId('purge-select-Order ID'))
    await user.click(screen.getByTestId('purge-selected'))

    const dialog = await screen.findByRole('alertdialog')
    expect(dialog.textContent).toMatch(/10,000 个实体/)  // 弹窗上说的是列表里的数，够用户判断规模
    expect(dialog.textContent).toMatch(/图谱里的节点和边/)
    expect(dialog.textContent).toMatch(/不删：实体类型本身的定义/)
    expect(dialog.textContent).toMatch(/无法撤销/)
  })

  it('弹窗里点取消，一条都不删', async () => {
    const user = userEvent.setup()
    renderPanel()
    await expand(user)
    await user.click(screen.getByTestId('purge-select-all'))
    await user.click(screen.getByTestId('purge-selected'))

    const dialog = await screen.findByRole('alertdialog')
    await user.click(within(dialog).getByRole('button', { name: '取消' }))

    expect(calls.filter((c) => c.url.endsWith('/terms/purge'))).toEqual([])
  })

  it('一个都没选时按钮不可点', async () => {
    const user = userEvent.setup()
    renderPanel()
    await expand(user)

    expect(screen.getByTestId('purge-selected').hasAttribute('disabled')).toBe(true)
  })

  it('中间某个类型失败时其余照常清完，失败的逐条报出来', async () => {
    // 整批回滚才是更糟的那个：用户要的是"清干净重导"，因为一个类型在这期间
    // 被人导入了新数据就让另外两个也留着，他只能再来一遍。
    purgeFailures = {
      订单号: { status: 409, detail: '「订单号」现在有 10005 个实体，确认时看到的是 10000 个' },
    }
    const user = userEvent.setup()
    const onPurged = renderPanel()
    await expand(user)
    await user.click(screen.getByTestId('purge-select-all'))
    await user.click(screen.getByTestId('purge-selected'))
    await confirmDialog(user)

    const outcomes = await screen.findByTestId('purge-outcomes')
    expect(outcomes.textContent).toMatch(/Order ID：已清空 10,001 个实体/)
    expect(outcomes.textContent).toMatch(/10005/)
    expect(outcomes.textContent).toMatch(/这个类型没有清空，其余照常/)
    expect(outcomes.textContent).toMatch(/Company：已清空 4 个实体/)
    expect(onPurged).toHaveBeenCalledWith(expect.stringContaining('2/3'))
  })
})
