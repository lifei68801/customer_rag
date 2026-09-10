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
  questions_source: 'handwritten' | 'generated' | 'unavailable'
}

let personaBody: PersonaBody
let staleBody: { stale: string[] }
let staleStatus = 200
let detailGetCount = 0
let putStatus = 200
let putDetail = ''
let putBodies: unknown[] = []

interface FaceBody {
  persona_id: string
  name: string
  avatar: string
  tagline: string
}
/** 这个租户挂着的脸（ADR-0004：一个租户可以挂多张）。 */
let faces: FaceBody[]
/** 除 default 之外各张脸的详情；default 那张走 personaBody。 */
let otherFaceDetails: Record<string, PersonaBody>
let faceCreateStatus = 201
let faceDeleteRequests: string[] = []
let faceDeleteStatus = 200
let faceDeleteDetail = ''

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
      // 详情 / 失效清单 / 脸的增删都挂在 /persona 下，且详情现在带
      // `?persona_id=` 查询串。按 pathname 精确分派，不用 includes——
      // includes('/persona') 会把列表 /personas 也吃进来。
      const parsed = new URL(url, 'http://x')
      const personaId = parsed.searchParams.get('persona_id') ?? 'default'
      if (parsed.pathname.endsWith('/persona/stale-questions')) {
        return Promise.resolve(
          new Response(JSON.stringify(staleStatus === 200 ? staleBody : {}), {
            status: staleStatus,
          }),
        )
      }
      if (parsed.pathname.endsWith('/persona/faces')) {
        if (method === 'POST') {
          const body = JSON.parse(String(init?.body ?? '{}')) as { persona_id: string; name: string }
          if (faceCreateStatus !== 201) {
            return Promise.resolve(
              new Response(JSON.stringify({ detail: '数字人 ID 不能为空' }), { status: faceCreateStatus }),
            )
          }
          const created = { persona_id: body.persona_id, name: body.name, avatar: '', tagline: '' }
          faces = [...faces, created]
          otherFaceDetails[body.persona_id] = {
            ...created,
            tenant_id: 'demo',
            questions: [],
            questions_source: 'generated',
          }
          return Promise.resolve(new Response(JSON.stringify(created), { status: 201 }))
        }
        return Promise.resolve(new Response(JSON.stringify({ faces }), { status: 200 }))
      }
      const deleteMatch = /\/persona\/faces\/([^/]+)$/.exec(parsed.pathname)
      if (deleteMatch && method === 'DELETE') {
        const deleted = decodeURIComponent(deleteMatch[1])
        faceDeleteRequests.push(deleted)
        if (faceDeleteStatus !== 200) {
          return Promise.resolve(
            new Response(JSON.stringify({ detail: faceDeleteDetail }), { status: faceDeleteStatus }),
          )
        }
        faces = faces.filter((f) => f.persona_id !== deleted)
        return Promise.resolve(new Response(JSON.stringify({ deleted: true }), { status: 200 }))
      }
      if (/\/api\/admin\/[^/]+\/persona$/.test(parsed.pathname)) {
        const target = personaId === 'default' ? personaBody : otherFaceDetails[personaId]
        if (method === 'PUT') {
          putBodies.push({ persona_id: personaId, ...JSON.parse(String(init?.body ?? '{}')) })
          if (putStatus !== 200) {
            return Promise.resolve(
              new Response(JSON.stringify({ detail: putDetail }), { status: putStatus }),
            )
          }
          return Promise.resolve(
            new Response(
              JSON.stringify({ ...target, ...JSON.parse(String(init?.body ?? '{}')) }),
              { status: 200 },
            ),
          )
        }
        if (target === undefined) {
          // 500 且没有 detail：服务端挂了这一类。走的是兜底文案那条路。
          return Promise.resolve(new Response(JSON.stringify({}), { status: 500 }))
        }
        detailGetCount += 1
        return Promise.resolve(new Response(JSON.stringify(target), { status: 200 }))
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
  staleStatus = 200
  detailGetCount = 0
  putStatus = 200
  putDetail = ''
  putBodies = []
  faces = [{ persona_id: 'default', name: '', avatar: '🛍️', tagline: '我知道商品、口味和产地' }]
  otherFaceDetails = {}
  faceCreateStatus = 201
  faceDeleteRequests = []
  faceDeleteStatus = 200
  faceDeleteDetail = ''
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

  it('失效检测没跑成功时就地留一条常驻提示，不是一闪而过', async () => {
    // 这一页有的是地方摆这句话。用 3 秒自动消失的 toast，等于赌用户那三秒
    // 正好在看屏幕——没看到的人会以为「一条都没失效」，而真相是根本没查。
    staleStatus = 500
    await renderPersonaEditor()
    await waitFor(() => expect(screen.getByText(/失效检测没跑成功/)).toBeTruthy())
    // 表单照常能用：那是一条附加提示，不是拦路的错误。
    expect(screen.getByRole('button', { name: '保存' })).toBeTruthy()
    // 而且要给得出重试的入口，光说坏了没有出路等于只完成一半。
    expect(screen.getByRole('button', { name: '重新检测' })).toBeTruthy()
  })

  it('保存成功之后不回头重拉详情，表单不会被一次网络抖动冲掉', async () => {
    // 保存成功再拉一次 GET，那次 GET 失败的话，刚存好的表单会被整页
    // 「加载失败」替掉——用户读到的是「保存失败」，而其实存成功了。
    const user = userEvent.setup()
    await renderPersonaEditor()
    await waitFor(() => expect(questionInputs()).toHaveLength(2))
    expect(detailGetCount).toBe(1)
    await user.click(screen.getByRole('button', { name: '保存' }))
    await waitFor(() => expect(putBodies).toHaveLength(1))
    expect(detailGetCount).toBe(1)
    expect(questionInputs()).toHaveLength(2)
  })

  it('删光引导问题时说清保存会发生什么，不说反', async () => {
    // 「前台会显示一个空的首屏」在还没保存的时候是假话——那会儿前台显示
    // 的仍是自动生成的那批。说反了比不说更糟：管理员据此以为不用保存。
    personaBody = { ...personaBody, questions: ['只有一条'], questions_source: 'generated' }
    const user = userEvent.setup()
    await renderPersonaEditor()
    await waitFor(() => expect(questionInputs()).toHaveLength(1))
    await user.click(screen.getByRole('button', { name: '删除第 1 条' }))
    expect(screen.getByText(/就这样保存/)).toBeTruthy()
    expect(screen.getByText(/不保存的话，前台继续显示/)).toBeTruthy()
  })

  it('图谱查不通时说是故障，不是装成「本体里没什么可问的」', async () => {
    // 对终端用户两者都是空引导区（诚实）；对管理员完全不同——后者是正常
    // 状态，前者是他该去修的故障，而他是唯一修得了的人。
    personaBody = { ...personaBody, questions: [], questions_source: 'unavailable' }
    await renderPersonaEditor()
    await waitFor(() => expect(screen.getByText(/图谱查不通/)).toBeTruthy())
    // 不能同时还说「一条引导问题都没有，就这样保存等于…」——那句话在
    // 这个状态下是误导：他没删过任何东西。
    expect(screen.queryByText(/就这样保存/)).toBeNull()
    // 也不能说成「这个数字人不显示任何引导问题」——那读起来像配置的结果，
    // 而它是一个故障。
    expect(screen.queryByText(/这个数字人不显示任何引导问题/)).toBeNull()
  })

  it('能新建一张脸并切过去编辑', async () => {
    // 没有这个入口，「一个租户挂多张脸」（ADR-0004）就没做完。
    const user = userEvent.setup()
    await renderPersonaEditor()
    await screen.findByDisplayValue('有哪些无香料的洗发水？')

    await user.type(screen.getByLabelText('新脸的 ID'), 'kefu')
    await user.type(screen.getByLabelText('新脸的名字'), '客服阿May')
    await user.click(screen.getByRole('button', { name: '新建这张脸' }))

    // 建完直接切过去编辑：选择器里多了它，且它是当前项；
    // 表单显示的是它自己的（空的）引导问题，不是 default 那张的。
    const tab = await screen.findByRole('tab', { name: /客服阿May/ })
    expect(tab.getAttribute('aria-selected')).toBe('true')
    await waitFor(() => expect(screen.queryByDisplayValue('有哪些无香料的洗发水？')).toBeNull())
    // 之后保存落到这张脸上，不是落回 default。
    await user.type(screen.getByLabelText('人设'), '售后的事问我')
    await user.click(screen.getByRole('button', { name: '保存' }))
    await waitFor(() => expect(putBodies).toHaveLength(1))
    expect((putBodies[0] as { persona_id: string }).persona_id).toBe('kefu')
  })

  it('切换编辑哪张脸时，引导问题跟着换', async () => {
    faces = [
      ...faces,
      { persona_id: 'kefu', name: '客服阿May', avatar: '🎧', tagline: '售后的事问我' },
    ]
    otherFaceDetails.kefu = {
      tenant_id: 'demo',
      name: '客服阿May',
      avatar: '🎧',
      tagline: '售后的事问我',
      questions: ['退货要几天？'],
      questions_source: 'handwritten',
    }
    const user = userEvent.setup()
    await renderPersonaEditor()
    await screen.findByDisplayValue('有哪些无香料的洗发水？')

    await user.click(screen.getByRole('tab', { name: /客服阿May/ }))

    await screen.findByDisplayValue('退货要几天？')
    // 上一张脸的问题不能还留在表单里——留着的话一保存就把它们写进这张脸。
    expect(screen.queryByDisplayValue('有哪些无香料的洗发水？')).toBeNull()
  })

  it('default 那张脸的删除按钮禁用并给出理由，不是点了静默失败', async () => {
    // 后端对它回 409（存量会话都挂在它下面）。前端禁掉只是不让人白点一次，
    // 但禁了必须说为什么——点不动且不说原因，用户会以为界面坏了。
    await renderPersonaEditor()
    await screen.findByDisplayValue('有哪些无香料的洗发水？')

    const remove = screen.getByRole('button', { name: /删除这张脸/ })
    expect(remove.hasAttribute('disabled')).toBe(true)
    expect(screen.getByText(/存量会话都挂在它下面/)).toBeTruthy()
    expect(faceDeleteRequests).toEqual([])
  })

  it('删掉当前编辑的那张脸之后回到 default', async () => {
    faces = [
      ...faces,
      { persona_id: 'kefu', name: '客服阿May', avatar: '🎧', tagline: '售后的事问我' },
    ]
    otherFaceDetails.kefu = {
      tenant_id: 'demo',
      name: '客服阿May',
      avatar: '🎧',
      tagline: '售后的事问我',
      questions: ['退货要几天？'],
      questions_source: 'handwritten',
    }
    const user = userEvent.setup()
    await renderPersonaEditor()
    await user.click(await screen.findByRole('tab', { name: /客服阿May/ }))
    await screen.findByDisplayValue('退货要几天？')

    await user.click(screen.getByRole('button', { name: /删除这张脸/ }))
    const dialog = await screen.findByRole('alertdialog')
    await user.click(within(dialog).getByRole('button', { name: '删除' }))

    await waitFor(() => expect(faceDeleteRequests).toEqual(['kefu']))
    // 表单回到 default 那张脸的内容，而不是停在一张已经不存在的脸上。
    await screen.findByDisplayValue('有哪些无香料的洗发水？')
    expect(screen.queryByRole('tab', { name: /客服阿May/ })).toBeNull()
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
