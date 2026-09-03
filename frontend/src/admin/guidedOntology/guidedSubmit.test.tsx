import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../../App'
import { SkinProvider } from '../SkinContext'
import { ConfirmProvider } from '../ConfirmContext'
import { ToastProvider } from '../ToastContext'
import { ADMIN_ROUTES } from '../../adminRoutes'

/**
 * 引导页第三步：把用户确认后的草案写进本体草稿，顺带产出 ETL 映射配置。
 *
 * 三条设计要点，每条都对应下面某条测试：
 * 1. 一次请求写入整套本体——逐个写的话中途失败会留下半份草稿，而 checkout
 *    不会清空它。
 * 2. 写入的是草稿，不是直接确认——确认是不可逆的（旧的已确认版本会被
 *    换掉），引导不该替用户做这个决定。
 * 3. 写入失败时不跳走，错误留在页面上——跳走的话用户以为成功了，回头
 *    发现草稿是空的。
 */

let replaceCalls: Array<{
  term_types: Array<{ value: string }>
  relation_types: unknown[]
  constraints: unknown[]
}> = []
let confirmCalls: string[] = []
let replaceStatus = 200
let replaceBody: unknown = { replaced: true }

function stubReplace(status: number, body: unknown) {
  replaceStatus = status
  replaceBody = body
}

function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const json = (body: unknown, status = 200) =>
        Promise.resolve(new Response(JSON.stringify(body), { status }))
      if (url.includes('/nav-badges')) {
        return json({ pending_relations: 0, pending_duplicates: 0, total_terms: 0 })
      }
      if (url.includes('/draft/replace')) {
        replaceCalls.push(JSON.parse(String(init?.body ?? '{}')))
        return json(replaceBody, replaceStatus)
      }
      if (url.includes('/confirm')) {
        confirmCalls.push(url)
        return json({}, 200)
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
  replaceCalls = []
  confirmCalls = []
  replaceStatus = 200
  replaceBody = { replaced: true }
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

// 订单号必须是非纯数字（"ORD" 前缀）：纯数字列会被 columnStats 的
// NUMERIC_IDENTIFIER_THRESHOLD 逻辑之外的路径判成 integer 类型，从而在
// classify() 里先走 measure 分支，永远轮不到"高比例=标识"的判定。10 行、
// 产品只有两个取值，让 产品 的重复度落进 DIMENSION_MAX_RATIO(0.2) 以内，
// 才会被判成 dimension（而不是"重复度不足以当分类"的 freetext）。
const csvFile = () =>
  new File(
    [
      '订单号,产品,revenue\n' +
        'ORD1001,咖啡,10.5\n' +
        'ORD1002,茶,20\n' +
        'ORD1003,咖啡,30\n' +
        'ORD1004,茶,15\n' +
        'ORD1005,咖啡,22\n' +
        'ORD1006,茶,18\n' +
        'ORD1007,咖啡,25\n' +
        'ORD1008,茶,12\n' +
        'ORD1009,咖啡,28\n' +
        'ORD1010,茶,19\n',
    ],
    'orders.csv',
    { type: 'text/csv' },
  )

/**
 * 走到第二步复核界面：登录、渲染引导页、传一张真实的表（走真实扫描），
 * 停在「写入草稿」按钮出现为止。
 *
 * 刻意不 await：brief 里的用例都是同步调用它，再靠 `await screen.findByRole`
 * 的轮询等它落地——跟 guidedPage.test.tsx 里其余用例的写法保持一致。
 */
async function renderAtReviewStep() {
  signIn('admin')
  const user = userEvent.setup()
  renderAt(ADMIN_ROUTES.guidedOntology)
  await user.upload(await screen.findByLabelText(/选择一张表/), csvFile())
}

describe('提交草稿', () => {
  it('一次请求写入整套本体', async () => {
    // 逐个写的话中途失败会留下半份草稿，而 checkout 不会清空它。
    const user = userEvent.setup()
    renderAtReviewStep()
    await user.click(await screen.findByRole('button', { name: /写入草稿/ }))
    await waitFor(() => expect(replaceCalls.length).toBe(1))
    const body = replaceCalls[0]
    expect(body.term_types.map((t) => t.value)).toContain('订单号')
    expect(body.constraints.length).toBeGreaterThan(0)
  })

  it('写入的是草稿，不是直接确认', async () => {
    // 确认是不可逆的（旧的已确认版本会被换掉）。引导不该替用户做这个
    // 决定。
    const user = userEvent.setup()
    renderAtReviewStep()
    await user.click(await screen.findByRole('button', { name: /写入草稿/ }))
    await waitFor(() => expect(replaceCalls.length).toBe(1))
    expect(confirmCalls.length).toBe(0)
  })

  it('写入失败时不跳走，错误留在页面上', async () => {
    // 跳走的话用户以为成功了，回头发现草稿是空的。
    stubReplace(400, { detail: '约束引用了未声明的实体类型：幽灵' })
    const user = userEvent.setup()
    renderAtReviewStep()
    await user.click(await screen.findByRole('button', { name: /写入草稿/ }))
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByRole('button', { name: /写入草稿/ })).toBeTruthy()
  })

  it('成功后提示下一步是确认，并给出去处', async () => {
    const user = userEvent.setup()
    renderAtReviewStep()
    await user.click(await screen.findByRole('button', { name: /写入草稿/ }))
    expect(await screen.findByRole('link', { name: /本体结构|去确认/ })).toBeTruthy()
  })

  it('成功后提供 ETL 映射下载，不用重配', async () => {
    // 引导收集的信息已经够生成映射了。让用户在 ETL 页把同样的判断再做
    // 一遍是重复劳动，而且两次结果可能不一致。
    const user = userEvent.setup()
    renderAtReviewStep()
    await user.click(await screen.findByRole('button', { name: /写入草稿/ }))
    expect(await screen.findByRole('button', { name: /映射配置|下载配置/ })).toBeTruthy()
  })
})
