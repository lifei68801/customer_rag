import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { TermTypePurgePanel } from './TermTypePurgePanel'

/**
 * 彻底清空一个实体类型。不可逆，所以这组测试钉的全是"闸"：
 * 列出摘要里看不见的类型、不输对名字按钮不可点、预览之后数据变了就不按新数字执行。
 */

type Call = { url: string; body: unknown }
let calls: Call[] = []
let storedTypes = [
  { term_type: 'Order ID', stored: 10000, visible: 0 },
  { term_type: 'Customer City', stored: 7769, visible: 7769 },
]
let previewCount = 10000
let purgeStatus = 200

function json(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status }))
}

beforeEach(() => {
  calls = []
  storedTypes = [
    { term_type: 'Order ID', stored: 10000, visible: 0 },
    { term_type: 'Customer City', stored: 7769, visible: 7769 },
  ]
  previewCount = 10000
  purgeStatus = 200
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const body = init?.body ? JSON.parse(String(init.body)) : null
      calls.push({ url, body })
      if (url.endsWith('/terms/stored-types')) return json({ types: storedTypes })
      if (url.endsWith('/terms/purge/preview')) {
        return json({ term_type: body.term_type, node_count: previewCount, stored_rows: previewCount, created_only: 0 })
      }
      if (url.endsWith('/terms/purge')) {
        if (purgeStatus === 409) {
          previewCount = 10005
          return json({ detail: '「Order ID」现在有 10005 个实体，确认时看到的是 10000 个' }, 409)
        }
        storedTypes = storedTypes.filter((t) => t.term_type !== body.term_type)
        return json({ node_count: body.expected_node_count, removed_by_table: { terms: 10000 } })
      }
      return new Promise(() => {})
    }),
  )
})

function renderPanel(onPurged = vi.fn()) {
  render(<TermTypePurgePanel sessionToken="s" tenantId="demo" onPurged={onPurged} />)
  return onPurged
}

async function openOrderIdPreview(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: /彻底清空某个实体类型/ }))
  await user.click(await screen.findByTestId('purge-open-Order ID'))
  return screen.findByTestId('purge-confirm')
}

describe('彻底清空实体类型', () => {
  it('列出全被删除、在分组摘要里看不见的类型，并说出重新导入不会恢复', async () => {
    const user = userEvent.setup()
    renderPanel()

    await user.click(screen.getByRole('button', { name: /彻底清空某个实体类型/ }))

    const row = (await screen.findByText('Order ID')).closest('tr')!
    expect(within(row).getByText(/10000 条已删除，重新导入不会恢复/)).toBeTruthy()
  })

  it('默认折叠，不展开不发请求', () => {
    renderPanel()

    expect(screen.queryByTestId('purge-open-Order ID')).toBeNull()
    expect(calls).toEqual([])
  })

  it('没有原样输入类型名，清空按钮不可点', async () => {
    const user = userEvent.setup()
    renderPanel()
    const confirm = await openOrderIdPreview(user)
    await within(confirm).findByText(/10000/)
    const execute = within(confirm).getByTestId('purge-execute')

    expect(execute.hasAttribute('disabled')).toBe(true)
    await user.type(within(confirm).getByRole('textbox'), 'order id')
    expect(execute.hasAttribute('disabled')).toBe(true)
  })

  it('输对名字后执行，带上预览时看到的数量和确认文字，完成后通知页面', async () => {
    const user = userEvent.setup()
    const onPurged = renderPanel()
    const confirm = await openOrderIdPreview(user)
    await within(confirm).findByText(/10000/)

    await user.type(within(confirm).getByRole('textbox'), 'Order ID')
    await user.click(within(confirm).getByTestId('purge-execute'))

    await waitFor(() => expect(onPurged).toHaveBeenCalledWith(expect.stringContaining('Order ID')))
    const purge = calls.find((c) => c.url.endsWith('/terms/purge'))!
    expect(purge.body).toEqual({ term_type: 'Order ID', expected_node_count: 10000, confirm_text: 'Order ID' })
    // 清空完刷新列表，那一行消失。
    await waitFor(() => expect(screen.queryByText('Order ID')).toBeNull())
  })

  it('预览之后数据变了（409）：不当成失败了事，重新拉预览让用户看新数字、重新确认', async () => {
    purgeStatus = 409
    const user = userEvent.setup()
    const onPurged = renderPanel()
    const confirm = await openOrderIdPreview(user)
    await within(confirm).findByText(/10000/)

    await user.type(within(confirm).getByRole('textbox'), 'Order ID')
    await user.click(within(confirm).getByTestId('purge-execute'))

    const refreshed = await screen.findByTestId('purge-confirm')
    expect(await within(refreshed).findByText('10005')).toBeTruthy()
    expect(within(refreshed).getByRole('alert').textContent).toMatch(/10005/)
    // 输入框清空，必须重新输入一次才能执行——不允许"刚才输过了"直接按新数字删。
    expect((within(refreshed).getByRole('textbox') as HTMLInputElement).value).toBe('')
    expect(within(refreshed).getByTestId('purge-execute').hasAttribute('disabled')).toBe(true)
    expect(onPurged).not.toHaveBeenCalled()
  })
})
