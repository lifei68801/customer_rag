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
 * 租户标记异常的关系边在详情页上的入口。
 *
 * 这些边（边自己的 tenant_id 跟两端节点对不上，或者两端节点分属不同租户）
 * 在正常那份关系清单里通常一条都不显示——那份按边的 tenant_id 过滤。它们
 * 照样挂在实体上，而且只能从这里删除。只有接口没有入口等于没修：脏边仍然
 * 是用户在界面上查不到、也处置不了的东西。
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
  relations: [],
}

const mismatchedEdge = {
  direction: 'out',
  relation_type: '生产',
  node_key: '产品:雪碧',
  standard_name: '雪碧',
  term_type: '产品',
  other_tenant_id: 'demo',
  edge_tenant_id: 'legacy_demo',
  category: 'edge_tenant_mismatch',
  deletable: true,
}

const crossTenantEdge = {
  direction: 'in',
  relation_type: '隶属',
  node_key: null,
  standard_name: null,
  term_type: null,
  other_tenant_id: null,
  edge_tenant_id: null,
  category: 'cross_tenant',
  deletable: false,
}

interface Call {
  url: string
  method: string
}

function stub(
  rows: unknown[],
  deleteResponse = () =>
    Promise.resolve(new Response(JSON.stringify({ deleted: 1 }), { status: 200 })),
) {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      calls.push({ url: decodeURIComponent(url), method })
      if (url.includes('/auth/whoami')) return whoamiResponse()
      if (method === 'DELETE') return deleteResponse()
      if (url.includes('/relations/inconsistent')) {
        return Promise.resolve(
          new Response(JSON.stringify({ inconsistent_relations: rows }), { status: 200 }),
        )
      }
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

async function openDetail() {
  render(
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
  await waitFor(() => expect(screen.getByTestId('term-detail')).toBeTruthy())
}

const page = () => within(screen.getByTestId('term-detail'))

describe('租户标记异常的关系边', () => {
  it('一条都没有时整栏都不出现', async () => {
    // 每个实体都挂一栏"异常边（0）"只会制造焦虑，还会把真出问题的那次淹掉。
    stub([])
    await openDetail()
    expect(page().queryByText(/租户标记异常/)).toBeNull()
  })

  it('列出这条边是什么，以及它到底哪儿不对', async () => {
    // 光说"有一条异常边"没用——用户要判断能不能删，得看到它连着谁、
    // 边上标的租户是什么。
    stub([mismatchedEdge])
    await openDetail()
    const section = within(page().getByTestId('inconsistent-relations'))
    expect(section.getByText(/可口可乐 -生产-> 雪碧/)).toBeTruthy()
    expect(section.getByText(/legacy_demo/)).toBeTruthy()
  })

  it('跨租户的那条不给删除按钮，并说清该找谁', async () => {
    // member 删不了它（后端会 403）。不给按钮、并说明原因，比让他点一下
    // 撞个 403 强——那看起来像是系统坏了。
    stub([crossTenantEdge])
    await openDetail()
    const section = within(page().getByTestId('inconsistent-relations'))
    expect(section.queryByRole('button', { name: /删除/ })).toBeNull()
    expect(section.getByText(/平台管理员/)).toBeTruthy()
  })

  it('确认后按这条边自己的两端发出删除请求', async () => {
    const calls = stub([mismatchedEdge])
    await openDetail()
    const section = within(page().getByTestId('inconsistent-relations'))
    await userEvent.click(
      section.getByRole('button', { name: '删除关系 可口可乐 -生产-> 雪碧' }),
    )
    const dialog = await screen.findByRole('alertdialog')
    // 删边不可逆，确认框必须把那条边原样写出来
    expect(dialog.textContent).toContain('可口可乐 -生产-> 雪碧')
    await userEvent.click(within(dialog).getByRole('button', { name: '确认' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'DELETE')).toBe(true))
    const deleted = calls.find((c) => c.method === 'DELETE')!
    expect(deleted.url).toContain(`/terms/${NODE_KEY}/relations/inconsistent`)
    expect(deleted.url).toContain('direction=out')
    expect(deleted.url).toContain('relation_type=生产')
    expect(deleted.url).toContain('other_node_key=产品:雪碧')
    // 对端节点的租户是定位这条边的一半——不带上它，后端匹配不到任何边
    expect(deleted.url).toContain('other_tenant_id=demo')
  })

  it('取消就什么都不删', async () => {
    const calls = stub([mismatchedEdge])
    await openDetail()
    const section = within(page().getByTestId('inconsistent-relations'))
    await userEvent.click(
      section.getByRole('button', { name: '删除关系 可口可乐 -生产-> 雪碧' }),
    )
    await userEvent.click(
      within(await screen.findByRole('alertdialog')).getByRole('button', { name: '取消' }),
    )
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(calls.filter((c) => c.method === 'DELETE')).toEqual([])
  })

  it('删除失败时把后端说的原因显示出来', async () => {
    // 静默失败：那条边还在，而用户以为删掉了。
    stub([mismatchedEdge], () =>
      Promise.resolve(
        new Response(JSON.stringify({ detail: '只有平台管理员能处理这一类边' }), { status: 403 }),
      ),
    )
    await openDetail()
    const section = within(page().getByTestId('inconsistent-relations'))
    await userEvent.click(
      section.getByRole('button', { name: '删除关系 可口可乐 -生产-> 雪碧' }),
    )
    await userEvent.click(
      within(await screen.findByRole('alertdialog')).getByRole('button', { name: '确认' }),
    )
    expect(await screen.findByText(/只有平台管理员能处理这一类边/)).toBeTruthy()
  })
})
