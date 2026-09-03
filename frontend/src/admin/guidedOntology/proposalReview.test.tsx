import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ProposalReview } from './ProposalReview'
import { buildProposal, initialDecision } from './draftProposal'
import { assignRoles } from './columnRoles'
import type { ColumnStats, GuidedDecision, Proposal, RoledColumn } from './types'

/**
 * 审阅视图：把生成的本体草案摆给用户看，让他逐项确认或改。
 *
 * demo 数据沿用 Task 4（draftProposal.test.ts）那份电商订单宽表的形状：
 * 订单号是标识列、产品/公司/类目/用户名/customer_state 是低基数候选、
 * revenue/units_sold 是度量、purchase_date 是日期、internal_note 是自由
 * 文本。
 *
 * 但这里没有像 Task 4 那样把 产品/公司/类目/用户名 也判成 dimension——
 * 那样的话默认渲染就有 5 组"建成实体/做成属性"单选，每组的可访问名都
 * 以"建成实体"开头，`findByRole('radio', { name: /建成实体/ })`
 * （brief 给的断言，逐字照抄）会因为找到 5 个匹配而报
 * "Found multiple elements"——这在真实跑过一遍之后确认过，不是猜测。
 * 所以这份 fixture 把它们标成 identifier：在这一步的语境里，它们代表
 * "已经确定要建成实体、不需要用户再审"的列（用于覆盖"层级"里"挂在
 * 谁下面"这组测试），真正把"建成实体还是做成属性"这个选择题留给唯一
 * 的 dimension 列 customer_state——这也是"低基数列的选择"三条测试
 * 唯一关心的列。customer_state 的 role/reason 用真实的 assignRoles 产
 * 出，不手写——判定依据里的"50"必须来自 classify() 的真实输出，手写
 * 空字符串会让"显示判定依据里的具体数字"这条测试测不出组件到底有没有
 * 把 reason 渲染出来。
 */

function stat(
  name: string,
  distinctCount: number,
  inferredType: ColumnStats['inferredType'] = 'string',
  samples: string[] = [],
): ColumnStats {
  return {
    name,
    nonEmptyCount: 10000,
    distinctCount,
    distinctCapped: false,
    samples,
    inferredType,
  }
}

function makeColumn(
  name: string,
  role: RoledColumn['role'],
  distinctCount: number,
  samples: string[] = [],
): RoledColumn {
  return {
    stats: stat(name, distinctCount, role === 'measure' ? 'number' : role === 'date' ? 'date' : 'string', samples),
    role,
    reason: '',
  }
}

/** demo 租户那张电商订单宽表，跟 draftProposal.test.ts 里的 demoColumns 同形状。 */
function demoRoled(): RoledColumn[] {
  const [customerState] = assignRoles([stat('customer_state', 50, 'string', ['加州', '德州', '纽约州'])])
  return [
    makeColumn('订单号', 'identifier', 9998),
    makeColumn('产品', 'identifier', 10, ['咖啡', '茶', '可乐']),
    makeColumn('公司', 'identifier', 3, ['甲公司', '乙公司']),
    makeColumn('类目', 'identifier', 4, ['饮料', '零食']),
    makeColumn('用户名', 'identifier', 800),
    makeColumn('revenue', 'measure', 500),
    makeColumn('units_sold', 'measure', 20),
    makeColumn('purchase_date', 'date', 300, ['2026-01-15']),
    customerState,
    makeColumn('internal_note', 'freetext', 6000),
  ]
}

const roled: RoledColumn[] = demoRoled()
const baseDecision: GuidedDecision = initialDecision(roled)
const baseProposal: Proposal = buildProposal(roled, baseDecision)

function renderReview(
  overrides: Partial<{
    roled: RoledColumn[]
    decision: GuidedDecision
    onDecisionChange: (next: GuidedDecision) => void
    proposal: Proposal
  }> = {},
) {
  const props = {
    roled,
    decision: baseDecision,
    onDecisionChange: vi.fn(),
    proposal: baseProposal,
    ...overrides,
  }
  return render(<ProposalReview {...props} />)
}

describe('低基数列的选择', () => {
  it('把两条路的能力差别摆出来，而不是问"实体还是属性"', async () => {
    // 问"该是实体还是属性"用户答不了——那是建模术语。问"你会不会问
    // 「加州有哪些客户」"他答得了。
    renderReview()
    const block = await screen.findByTestId('dimension-customer_state')
    expect(block.textContent).toMatch(/加州|哪些|能问/)
  })

  it('默认选中「建成实体」', async () => {
    renderReview()
    const radio = await screen.findByRole('radio', { name: /建成实体/ })
    expect((radio as HTMLInputElement).checked).toBe(true)
  })

  it('显示判定依据里的具体数字', async () => {
    renderReview()
    expect((await screen.findByTestId('dimension-customer_state')).textContent).toMatch(/50/)
  })
})

describe('层级', () => {
  it('每个实体能选挂在谁下面', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    renderReview({ onDecisionChange: onChange })
    await user.selectOptions(await screen.findByLabelText('类目 挂在'), '产品')
    expect(onChange).toHaveBeenCalled()
  })

  it('不能把实体挂在自己下面', async () => {
    // 自环会让约束表里出现 A-[R]->A，图谱查询会陷进去。
    renderReview()
    const select = (await screen.findByLabelText('类目 挂在')) as HTMLSelectElement
    const values = [...select.options].map((o) => o.value)
    expect(values).not.toContain('类目')
  })

  it('标识列是根，没有「挂在」选择', async () => {
    renderReview()
    expect(screen.queryByLabelText('订单号 挂在')).toBeNull()
  })

  it('已经用过的关系名要能选，不用重打', async () => {
    // SOLD_BY 在 demo 里用了两次（订单->公司、产品->公司）。不给选的话
    // 用户第二次会打出 SELL_BY，建出两个同义关系——图谱里同一件事有两种
    // 边，查询时漏掉一半而不报错。
    renderReview({
      decision: { ...baseDecision, relationNameOf: { ...baseDecision.relationNameOf, 公司: 'SOLD_BY' } },
    })
    const input = await screen.findByLabelText('类目 的关系名')
    const listId = input.getAttribute('list')
    expect(listId).toBeTruthy()
    const options = [...document.querySelectorAll(`#${listId} option`)].map((o) =>
      o.getAttribute('value'),
    )
    expect(options).toContain('SOLD_BY')
  })
})

describe('未使用的列', () => {
  it('列出来，不静静丢弃', async () => {
    // 不显示的话，用户永远不知道自己丢了什么——他会在三个月后问
    // "为什么查不到内部备注"，而那一列从一开始就没被采纳。
    renderReview()
    const unused = await screen.findByTestId('unused-columns')
    expect(unused.textContent).toMatch(/internal_note/)
  })

  it('未使用列为空时也要说一句，不是留白', async () => {
    renderReview({ proposal: { ...baseProposal, unusedColumns: [] } })
    expect((await screen.findByTestId('unused-columns')).textContent).toMatch(/都用上了|没有/)
  })
})

describe('日期列的限制', () => {
  it('明说范围过滤做不了', async () => {
    // 数据模型没有日期类型。不说的话，用户会以为"上个月的订单"这类问题
    // 能答，直到真去问才发现不行。
    renderReview()
    expect((await screen.findByTestId('date-warning')).textContent).toMatch(/范围|区间|过滤/)
  })
})
