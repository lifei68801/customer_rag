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
 * 数字人编辑页：这个知识库对外是谁、可以问它什么。
 *
 * 这一页最要命的两件事都在「被拒之后」：后端点名了是哪几条问题不成立，
 * 界面必须原样转达（包装成「保存失败」等于把用户唯一能用的信息扔掉）；
 * 以及被拒之后输入框里的内容必须还在（清空的话，配了六条被拒一条的人
 * 要全部重打）。
 */
interface PersonaBody {
  tenant_id: string
  name: string
  avatar: string
  tagline: string
  questions: string[]
  questions_source: 'handwritten' | 'generated'
}

let personaBody: PersonaBody
let staleBody: { stale: string[] }
let putStatus = 200
let putDetail = ''
let putBodies: unknown[] = []

function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      if (url.includes('/auth/whoami')) {
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
      // 失效清单的路径比详情长一段。详情那条正则用的是 `/persona$`，眼下
      // 吃不到它；先分派仍然更稳——正则哪天松成 includes 就出事。
      if (url.includes('/persona/stale-questions')) {
        return Promise.resolve(new Response(JSON.stringify(staleBody), { status: 200 }))
      }
      if (/\/api\/admin\/[^/]+\/persona$/.test(url)) {
        if (method === 'PUT') {
          putBodies.push(JSON.parse(String(init?.body ?? '{}')))
          if (putStatus !== 200) {
            return Promise.resolve(
              new Response(JSON.stringify({ detail: putDetail }), { status: putStatus }),
            )
          }
          return Promise.resolve(
            new Response(
              JSON.stringify({ ...personaBody, ...JSON.parse(String(init?.body ?? '{}')) }),
              { status: 200 },
            ),
          )
        }
        if (personaBody === undefined) {
          // 500 且没有 detail：服务端挂了这一类。走的是兜底文案那条路。
          return Promise.resolve(new Response(JSON.stringify({}), { status: 500 }))
        }
        return Promise.resolve(new Response(JSON.stringify(personaBody), { status: 200 }))
      }
      if (url.includes('/api/admin/personas')) {
        return Promise.resolve(
          new Response(JSON.stringify({ personas: [], current_tenant_id: 'demo' }), { status: 200 }),
        )
      }
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  personaBody = {
    tenant_id: 'demo',
    name: '导购小美',
    avatar: '🛍️',
    tagline: '我知道商品、口味和产地',
    questions: ['有哪些无香料的洗发水？', '哪些商品产自日本？'],
    questions_source: 'handwritten',
  }
  staleBody = { stale: [] }
  putStatus = 200
  putDetail = ''
  putBodies = []
  resetAdminSession()
  localStorage.clear()
  stubApi()
})

async function renderPersonaEditor() {
  const result = render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[ADMIN_ROUTES.persona]}>
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
  await screen.findByTestId('admin-topbar')
  return result
}

const questionInputs = () =>
  screen.getAllByRole('textbox', { name: /^引导问题第 \d+ 条$/ }) as HTMLInputElement[]

describe('数字人编辑页', () => {
  it('保存被拒时把后端点名的那几条原样显示出来', async () => {
    // 「…点了大概率答不出来：库存多少？」这句话里有用户需要的全部信息。
    // 包装成「保存失败」等于把它扔掉。
    putStatus = 400
    putDetail = '这几条引导问题在当前本体里一个已知名字都没提到，点了大概率答不出来：库存多少？'
    const user = userEvent.setup()
    await renderPersonaEditor()
    await user.click(await screen.findByRole('button', { name: '保存' }))
    await waitFor(() => expect(screen.getByText(/库存多少？/)).toBeTruthy())
  })

  it('保存被拒之后输入框里的内容还在', async () => {
    // 清空的话，配了六条被拒一条的人要全部重打。
    putStatus = 400
    putDetail = '这几条引导问题在当前本体里一个已知名字都没提到，点了大概率答不出来：库存多少？'
    const user = userEvent.setup()
    await renderPersonaEditor()
    await waitFor(() => expect(questionInputs()).toHaveLength(2))
    await user.click(screen.getByRole('button', { name: '保存' }))
    await waitFor(() => expect(screen.getByText(/库存多少？/)).toBeTruthy())
    expect(questionInputs().map((i) => i.value)).toEqual([
      '有哪些无香料的洗发水？',
      '哪些商品产自日本？',
    ])
    expect((screen.getByRole('textbox', { name: '人设' }) as HTMLInputElement).value).toBe(
      '我知道商品、口味和产地',
    )
  })

  it('能加一条、能删一条、能调顺序', async () => {
    // 顺序是有意义的——第一条占的位置最值钱。
    const user = userEvent.setup()
    await renderPersonaEditor()
    await waitFor(() => expect(questionInputs()).toHaveLength(2))

    await user.click(screen.getByRole('button', { name: '添加一条引导问题' }))
    expect(questionInputs()).toHaveLength(3)
    await user.type(questionInputs()[2], '产自哪里？')

    await user.click(screen.getByRole('button', { name: '上移第 3 条' }))
    expect(questionInputs().map((i) => i.value)).toEqual([
      '有哪些无香料的洗发水？',
      '产自哪里？',
      '哪些商品产自日本？',
    ])

    await user.click(screen.getByRole('button', { name: '删除第 1 条' }))
    expect(questionInputs().map((i) => i.value)).toEqual(['产自哪里？', '哪些商品产自日本？'])

    // 存出去的必须是调整后的顺序和内容，不是加载时那份——只断言界面的话，
    // 「界面动了但要存的那份没动」这种实现照样能绿。
    await user.click(screen.getByRole('button', { name: '保存' }))
    await waitFor(() => expect(putBodies).toHaveLength(1))
    expect((putBodies[0] as { questions: string[] }).questions).toEqual([
      '产自哪里？',
      '哪些商品产自日本？',
    ])
  })

  it('连按两次上移，是同一条一路往上，不是来回跳', async () => {
    // 鼠标用户每次都重新看一眼再点，位置错乱看不出来。键盘用户不一样：
    // 焦点停在按钮上连按两下 Enter，如果焦点不跟着被移动的那一条走，
    // 第二下动的就是刚被挤下来的那一条——两下之后原地不动。
    personaBody = { ...personaBody, questions: ['一', '二', '三'] }
    const user = userEvent.setup()
    await renderPersonaEditor()
    await waitFor(() => expect(questionInputs()).toHaveLength(3))

    screen.getByRole('button', { name: '上移第 3 条' }).focus()
    await user.keyboard('{Enter}')
    await user.keyboard('{Enter}')
    expect(questionInputs().map((i) => i.value)).toEqual(['三', '一', '二'])
  })

  it('最多六条，加到第七条时按钮禁用并说明为什么', async () => {
    // 「已达上限 6 条」而不是一个点不动的按钮。点不动且不说原因，用户会
    // 以为界面坏了。
    personaBody = { ...personaBody, questions: ['一', '二', '三', '四', '五'] }
    const user = userEvent.setup()
    await renderPersonaEditor()
    await waitFor(() => expect(questionInputs()).toHaveLength(5))

    const addButton = () => screen.getByRole('button', { name: '添加一条引导问题' }) as HTMLButtonElement
    expect(addButton().disabled).toBe(false)
    await user.click(addButton())
    expect(questionInputs()).toHaveLength(6)
    expect(addButton().disabled).toBe(true)
    // 光禁用不够，得说清为什么。
    expect(screen.getByText(/已达上限 6 条/)).toBeTruthy()
  })

  it('已经失效的引导问题显示一个警示，不是静默留着', async () => {
    // 本体改了之后那条问题不再命中——编辑页要标出来，否则管理员永远不
    // 知道自己配的问题已经答不出来了。
    staleBody = { stale: ['哪些商品产自日本？'] }
    await renderPersonaEditor()
    await waitFor(() => expect(screen.getByText(/已失效/)).toBeTruthy())
    // 标记必须挂在失效那一条上，不是页面顶上飘一句。挂错行的话，六条里
    // 哪条坏了仍然要靠猜。
    expect(within(screen.getByTestId('question-row-1')).getByText(/已失效/)).toBeTruthy()
    expect(within(screen.getByTestId('question-row-0')).queryByText(/已失效/)).toBeNull()
  })

  it('自动生成的那批要标明来源，不能冒充管理员配过的', async () => {
    // GET 的 questions 是「手写优先、没有就自动兜底」。分不清的话，管理员
    // 看到的是一组他从没配过的问题，一按保存就固化成手写、从此不再随本体
    // 变化——而界面全程没说过这件事。
    personaBody = { ...personaBody, questions_source: 'generated' }
    await renderPersonaEditor()
    await waitFor(() => expect(screen.getByText(/根据本体自动生成/)).toBeTruthy())
  })

  it('详情拉取失败时说出来，不是给一张空表单', async () => {
    // 空表单会被读成「还没配过」。照着它保存一次，真配过的内容就被空值
    // 覆盖了。
    personaBody = undefined as unknown as PersonaBody
    await renderPersonaEditor()
    await waitFor(() => expect(screen.getByText(/数字人信息加载失败/)).toBeTruthy())
    expect(screen.queryByRole('button', { name: '保存' })).toBeNull()
  })
})
