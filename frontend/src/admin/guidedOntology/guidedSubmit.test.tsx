import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../../App'
import { SkinProvider } from '../SkinContext'
import { ConfirmProvider } from '../ConfirmContext'
import { ToastProvider } from '../ToastContext'
import { ADMIN_ROUTES } from '../../adminRoutes'
import { resetAdminSession } from '../useAdminAuth'

/**
 * 身份不再存 sessionStorage（token 在 HttpOnly Cookie 里，JS 读不到，也
 * 塞不进去）：界面从 whoami 拿身份，所以这里要打桩的是 whoami。
 */
let signedInRole: 'admin' | 'member' | null = null

function whoamiResponse() {
  if (signedInRole === null) {
    return Promise.resolve(new Response(JSON.stringify({ detail: '未登录' }), { status: 401 }))
  }
  return Promise.resolve(
    new Response(
      JSON.stringify({
        username: signedInRole === 'admin' ? 'admin' : 'alice',
        role: signedInRole,
        tenant_id: signedInRole === 'admin' ? null : 'demo',
        current_tenant_id: 'demo',
      }),
      { status: 200 },
    ),
  )
}

function signIn(role: 'admin' | 'member') {
  signedInRole = role
}

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
// 跟 replaceCalls 分开存：replaceCalls 已经 JSON.parse 过，只保留断言用得上
// 的几个字段；submitGuidedFlow() 要把原始请求（含未解析的 body 字符串）
// 交回给调用方，让它自己按需 JSON.parse。
let rawReplaceRequests: RequestInit[] = []
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
      if (url.includes('/auth/whoami')) return whoamiResponse()
      const json = (body: unknown, status = 200) =>
        Promise.resolve(new Response(JSON.stringify(body), { status }))
      if (url.includes('/nav-badges')) {
        return json({ pending_relations: 0, pending_duplicates: 0, total_terms: 0 })
      }
      if (url.includes('/draft/replace')) {
        replaceCalls.push(JSON.parse(String(init?.body ?? '{}')))
        rawReplaceRequests.push(init ?? {})
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

beforeEach(() => {
  signedInRole = null
  resetAdminSession()
  sessionStorage.clear()
  localStorage.clear()
  replaceCalls = []
  rawReplaceRequests = []
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

// 订单号必须是非纯数字（"ORD" 前缀）：这份 fixture 只有 10 行，纯数字列
// 会保持 integer 类型，而 columnRoles 的 INTEGER_IDENTIFIER_MIN_ROWS(=20)
// 要求非空行数至少 20 才肯把整数列判成标识——10 行怎么判都够不上这道
// 门槛，中心就没了。字符串标识不受这道行数下限约束，用 ORD 前缀绕开它。
// （旧注释说这是因为 integer 在 classify() 里先走 measure 分支，那条短路
// 在 60ce3b7 就删掉了——纯数字列不能用的真实原因是行数，不是类型短路。）
// 10 行、产品只有两个取值，让 产品 的重复度落进 DIMENSION_MAX_RATIO(0.2)
// 以内，才会被判成 dimension（而不是"重复度不足以当分类"的 freetext）。
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

/**
 * 走完整条提交路径：登录、传表、点「写入草稿」，等 /draft/replace 落地，
 * 返回那次请求的原始 RequestInit（body 还是未解析的字符串，调用方自己
 * JSON.parse 取需要的字段）。
 */
async function submitGuidedFlow(): Promise<RequestInit> {
  const user = userEvent.setup()
  await renderAtReviewStep()
  await user.click(await screen.findByRole('button', { name: /写入草稿/ }))
  await waitFor(() => expect(rawReplaceRequests.length).toBe(1))
  return rawReplaceRequests[0]
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

  it('本体是空的（没有标识列，唯一的维度列也被改成了属性）时，写入草稿会被挡住，不会 POST 出去', async () => {
    // 跟 N1 是同一条路径的两个环节：没有标识列时中心是猜的（第一个维度
    // 列），用户完全可以把它也改判成属性，本体里就一个实体都不剩。
    // handleSubmit 之前对此没有设防——点「写入草稿」会把一份空本体 POST
    // 到 /draft/replace。这条测的是"挡住"这个动作本身，不是文案。
    const noIdentifierCsv = () =>
      new File(
        [
          '产品,revenue\n' +
            '咖啡,10.5\n咖啡,11.5\n咖啡,12.5\n咖啡,13.5\n咖啡,14.5\n' +
            '茶,20.5\n茶,21.5\n茶,22.5\n茶,23.5\n茶,24.5\n',
        ],
        'products.csv',
        { type: 'text/csv' },
      )
    signIn('admin')
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.guidedOntology)
    await user.upload(await screen.findByLabelText(/选择一张表/), noIdentifierCsv())

    const block = await screen.findByTestId('dimension-产品')
    await user.click(within(block).getByRole('radio', { name: /做成属性/ }))

    await user.click(await screen.findByRole('button', { name: /写入草稿/ }))
    const pageError = await screen.findByTestId('page-error')
    expect(pageError.textContent).toMatch(/一个实体都没有/)
    expect(replaceCalls.length).toBe(0)
  })

  it('写入草稿时把 ETL 映射一并提交，不让用户自己保管文件', async () => {
    // 映射是系统自己算出来的。让用户下载成文件再传回来，中途会关标签页、
    // 会在下载目录里找不着、过两天会分不清哪个 YAML 对应哪个租户。
    const posted = await submitGuidedFlow()
    const body = JSON.parse(posted.body as string)
    expect(body.etl_mapping.source_file_name).toBe('orders.csv')
    expect(body.etl_mapping.config_yaml).toMatch(/entities:/)
  })
})
