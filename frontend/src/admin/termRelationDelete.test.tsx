import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { resetAdminSession } from './useAdminAuth'

/**
 * 删关系边的入口。
 *
 * 在这之前后台没有任何地方能去掉一条已入图的边，而删实体又被"图谱里还有
 * 边"挡着——用户被困在里面。删边不可逆，所以按钮必须能分辨删的是哪条，
 * 确认框必须把那条边原样写出来。
 */

function whoamiResponse() {
  return Promise.resolve(
    new Response(
      JSON.stringify({
        username: 'alice',
        role: 'member',
        tenant_id: 'demo',
        current_tenant_id: 'demo',
      }),
      { status: 200 },
    ),
  )
}

const NODE_KEY = '公司:可口可乐'
const PATH = `/admin/terms/${encodeURIComponent(NODE_KEY)}`

const term = {
  node_key: NODE_KEY,
  standard_name: '可口可乐',
  aliases: [],
  term_type: '公司',
  extra_properties: {},
  source: 'etl',
  relations: [
    { direction: 'out', relation_type: '生产', node_key: '产品:雪碧', standard_name: '雪碧', term_type: '产品' },
    { direction: 'in', relation_type: '隶属', node_key: '类目:饮料', standard_name: '饮料', term_type: '类目' },
  ],
}

interface Call {
  url: string
  method: string
}

function stub(deleteResponse = () => Promise.resolve(new Response(JSON.stringify({ deleted: 1 }), { status: 200 }))) {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      calls.push({ url: decodeURIComponent(url), method })
      if (url.includes('/auth/whoami')) return whoamiResponse()
      if (method === 'DELETE') return deleteResponse()
      if (url.includes('/terms/')) {
        return Promise.resolve(new Response(JSON.stringify(term), { status: 200 }))
      }
      return new Promise(() => {})
    }),
  )
  return calls
}

beforeEach(() => {
  resetAdminSession()
  localStorage.clear()
})

function renderPage() {
  return render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[PATH]}>
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
}

const page = () => within(screen.getByTestId('term-detail'))

async function openDetail() {
  renderPage()
  await waitFor(() => expect(screen.getByTestId('term-detail')).toBeTruthy())
}

describe('删除关系边', () => {
  it('每条关系的删除按钮各自点名它删的是哪条', async () => {
    // 一排一模一样的"删除"按钮，用户（和读屏软件）分不出按下去掉的是哪条边。
    stub()
    await openDetail()
    expect(page().getByRole('button', { name: '删除关系 可口可乐 -生产-> 雪碧' })).toBeTruthy()
    expect(page().getByRole('button', { name: '删除关系 饮料 -隶属-> 可口可乐' })).toBeTruthy()
  })

  it('确认框把要删掉的那条边原样写出来', async () => {
    stub()
    await openDetail()
    await userEvent.click(page().getByRole('button', { name: '删除关系 可口可乐 -生产-> 雪碧' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog.textContent).toContain('可口可乐 -生产-> 雪碧')
    expect(dialog.textContent).toContain('不可撤销')
  })

  it('取消就什么都不删', async () => {
    // 破坏性操作误触的代价是不可逆的：取消之后不能有任何 DELETE 发出去。
    const calls = stub()
    await openDetail()
    await userEvent.click(page().getByRole('button', { name: '删除关系 可口可乐 -生产-> 雪碧' }))
    await userEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: '取消' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(calls.filter((c) => c.method === 'DELETE')).toEqual([])
  })

  it('确认后按方向发出删除请求', async () => {
    const calls = stub()
    await openDetail()
    // 入边：主语是对端。方向传错会删掉双向关系里用户没点的那一条。
    await userEvent.click(page().getByRole('button', { name: '删除关系 饮料 -隶属-> 可口可乐' }))
    await userEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: '确认' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'DELETE')).toBe(true))
    const deleted = calls.find((c) => c.method === 'DELETE')!
    expect(deleted.url).toContain(`/terms/${NODE_KEY}/relations`)
    expect(deleted.url).toContain('direction=in')
    expect(deleted.url).toContain('relation_type=隶属')
    expect(deleted.url).toContain('other_node_key=类目:饮料')
  })

  it('删除失败时把后端说的原因显示出来', async () => {
    // 边没删掉却什么都不说，用户会以为删成功了——刷新之后那条边还在。
    stub(() =>
      Promise.resolve(
        new Response(JSON.stringify({ detail: '没有找到这条关系（A -隶属-> B），它可能已经被删掉了' }), {
          status: 404,
        }),
      ),
    )
    await openDetail()
    await userEvent.click(page().getByRole('button', { name: '删除关系 饮料 -隶属-> 可口可乐' }))
    await userEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: '确认' }))
    expect(await screen.findByText(/没有找到这条关系/)).toBeTruthy()
  })
})
