import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { ADMIN_ROUTES } from '../adminRoutes'
import { resetAdminSession } from './useAdminAuth'

/**
 * 报错明细页。
 *
 * 三个来源的修复动作完全不同——文档失败去**重试**，表格跳行去**改表格**，
 * 问答未命中去**建模**——所以它们分成三个页签，压平成一个"错误列表"等于
 * 让用户自己猜该干什么。
 */

interface Fixture {
  counts: { documents: number; etl_rows: number; qa: number }
  documents: unknown[]
  etlRows: unknown[]
  qa: unknown[]
  totals: { documents: number; etlRows: number; qa: number }
  failing: Set<string>
}

/** 记下每个请求的完整 URL，用来断言分页参数和下载动作。 */
let requestedUrls: string[] = []

let fixture: Fixture

function baseFixture(): Fixture {
  return {
    counts: { documents: 2, etl_rows: 3, qa: 1 },
    documents: [
      {
        job_id: 'j1',
        file_path: '/uploads/年报.pdf',
        last_error: 'OCR 超时（120s）',
        attempts: 3,
        updated_at: '2026-09-08 10:00:00',
      },
    ],
    etlRows: [
      {
        id: 1,
        run_id: 'run-1',
        label: '产品',
        source_file: '商品表.csv',
        row_number: 88,
        reason: '价格非数字',
        created_at: '2026-09-08 10:00:00',
      },
    ],
    qa: [
      {
        id: 7,
        session_id: 's1',
        question: '库存多少',
        answer: '没找到确切答案',
        outcome: 'no_match',
        created_at: '2026-09-08 10:00:00',
      },
    ],
    totals: { documents: 1, etlRows: 1, qa: 1 },
    failing: new Set<string>(),
  }
}

function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
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
      requestedUrls.push(url)
      if (url.includes('/errors/etl-rows.csv')) {
        return Promise.resolve(new Response('run_id,label', { status: 200 }))
      }
      const routes: [string, () => unknown][] = [
        ['/errors/counts', () => fixture.counts],
        ['/errors/documents', () => ({ items: fixture.documents, total: fixture.totals.documents })],
        ['/errors/etl-rows', () => ({ items: fixture.etlRows, total: fixture.totals.etlRows })],
        ['/errors/qa', () => ({ items: fixture.qa, total: fixture.totals.qa })],
      ]
      for (const [suffix, body] of routes) {
        if (url.includes(suffix)) {
          if (fixture.failing.has(suffix)) {
            return Promise.resolve(
              new Response(JSON.stringify({ detail: `${suffix} 拉取失败` }), { status: 503 }),
            )
          }
          return Promise.resolve(new Response(JSON.stringify(body()), { status: 200 }))
        }
      }
      if (url.includes('/nav-badges')) {
        return Promise.resolve(new Response(JSON.stringify({}), { status: 200 }))
      }
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  fixture = baseFixture()
  requestedUrls = []
  resetAdminSession()
  localStorage.clear()
  stubApi()
})

async function renderPage() {
  const result = render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[ADMIN_ROUTES.errors]}>
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
  await screen.findByTestId('admin-topbar')
  return result
}

describe('报错明细页', () => {
  it('三个分页都在，角标各不相同', async () => {
    await renderPage()

    const tabs = await screen.findAllByRole('tab')
    const labels = tabs.map((tab) => tab.textContent)
    expect(labels).toHaveLength(3)
    // 角标必须来自各自的计数，不是同一个数字抄三遍。
    expect(labels[0]).toMatch(/文档.*2/)
    expect(labels[1]).toMatch(/表格.*3/)
    expect(labels[2]).toMatch(/问答.*1/)
  })

  it('某一类为 0 时角标写 0，不是不显示', async () => {
    // 「这一类没有问题」是用户需要看到的结论。不渲染的话它和"还没拉到"
    // 长得一模一样。
    fixture.counts = { documents: 0, etl_rows: 0, qa: 0 }
    await renderPage()

    const tabs = await screen.findAllByRole('tab')
    expect(tabs.map((t) => t.textContent)).toEqual([
      expect.stringMatching(/文档.*0/),
      expect.stringMatching(/表格.*0/),
      expect.stringMatching(/问答.*0/),
    ])
  })

  it('文档失败那一行把具体错误写出来', async () => {
    // 「OCR 超时」而不是「失败」——用户据此判断是重试还是换个文件。
    await renderPage()

    await waitFor(() => expect(screen.getByText(/OCR 超时（120s）/)).toBeTruthy())
    expect(screen.getByText(/年报\.pdf/)).toBeTruthy()
  })

  it('表格跳行那一行写清楚是哪个文件的第几行、为什么', async () => {
    await renderPage()

    await user_clickTab('表格')
    await waitFor(() => expect(screen.getByText(/商品表\.csv/)).toBeTruthy())
    expect(screen.getByText(/第 88 行/)).toBeTruthy()
    expect(screen.getByText(/价格非数字/)).toBeTruthy()
  })

  it('问答未命中那一行能一键跳到本体结构页', async () => {
    await renderPage()

    await user_clickTab('问答')
    const link = await screen.findByRole('link', { name: /去建模/ })
    expect(link.getAttribute('href')).toContain(ADMIN_ROUTES.ontology)
    // 带上那个问题：到了本体页还要自己回忆刚才问的是什么，这个入口就白给了。
    expect(decodeURIComponent(link.getAttribute('href') ?? '')).toContain('库存多少')
  })

  it('某一类为 0 时那一页说清楚，不是空白', async () => {
    // 空白让人以为没加载出来。
    fixture.counts = { documents: 0, etl_rows: 0, qa: 0 }
    fixture.documents = []
    await renderPage()

    await waitFor(() => expect(screen.getByText(/没有失败的文档/)).toBeTruthy())
  })

  it('只列出了一部分时说出来，并且能显示更多', async () => {
    // 不说的话，用户眼里的世界就只有这几十条——他会以为问题就这么大，
    // 而剩下的既不在任何页签里，也没有任何信号让人怀疑它们存在。
    fixture.totals = { documents: 250, etlRows: 1, qa: 1 }
    const user = userEvent.setup()
    await renderPage()

    await waitFor(() => expect(screen.getByText(/只列出了前 1 \/ 共 250 条/)).toBeTruthy())

    requestedUrls = []
    await user.click(screen.getByRole('button', { name: '显示更多' }))

    await waitFor(() =>
      expect(requestedUrls.some((u) => u.includes('/errors/documents?limit=100'))).toBe(true),
    )
  })

  it('表格跳行那一页能把整批下下来', async () => {
    // 要改的表格在用户自己电脑上。让他对着屏幕手抄两百个行号的话，
    // 这个功能等于没做。
    const clicks: string[] = []
    const realCreate = document.createElement.bind(document)
    vi.spyOn(document, 'createElement').mockImplementation((tag: string) => {
      const el = realCreate(tag)
      if (tag === 'a') el.click = () => clicks.push((el as HTMLAnchorElement).download)
      return el
    })
    globalThis.URL.createObjectURL = () => 'blob:fake'
    globalThis.URL.revokeObjectURL = () => {}
    await renderPage()

    await user_clickTab('表格')
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: /下载这批/ }))

    await waitFor(() =>
      expect(requestedUrls.some((u) => u.includes('/errors/etl-rows.csv'))).toBe(true),
    )
    expect(clicks).toEqual(['etl_skipped_rows.csv'])
    vi.restoreAllMocks()
  })

  it('某一个来源拉取失败只影响那一页', async () => {
    // 一个来源挂了就把整页换成错误的话，另外两类的问题也一起看不见了
    // ——而它们跟这次故障毫无关系。
    fixture.failing.add('/errors/documents')
    await renderPage()

    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())

    await user_clickTab('表格')
    await waitFor(() => expect(screen.getByText(/商品表\.csv/)).toBeTruthy())
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

async function user_clickTab(label: string) {
  const user = userEvent.setup()
  const tabs = await screen.findAllByRole('tab')
  const target = tabs.find((tab) => tab.textContent?.includes(label))
  if (!target) throw new Error(`没有找到「${label}」这个分页`)
  await user.click(target)
  await waitFor(() => expect(target.getAttribute('aria-selected')).toBe('true'))
}
