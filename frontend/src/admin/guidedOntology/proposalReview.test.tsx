import { describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ProposalReview } from './ProposalReview'
import { buildProposal, initialDecision } from './draftProposal'
import { assignRoles } from './columnRoles'
import type { ColumnStats, GuidedDecision, Proposal, RoledColumn } from './types'

/**
 * 审阅视图：把生成的本体草案摆给用户看，让他逐项确认或改。
 *
 * demo 数据是 Task 4（draftProposal.test.ts）那份电商订单宽表的完整形状：
 * 订单号是标识列，产品/公司/类目/用户名/customer_state 五列全部走真实
 * assignRoles 判成 dimension，revenue/units_sold 是度量，purchase_date
 * 是日期，internal_note 是自由文本。
 *
 * 复审那一轮发现：5 个 dimension 列意味着默认渲染有 5 组"建成实体/做成
 * 属性"单选，每组的可访问名都以"建成实体"开头。裸的
 * `screen.findByRole('radio', { name: /建成实体/ })` 会因为 5 个都匹配
 * 而报 "Found multiple elements"。保留多维度是为了让审阅视图在测试里
 * 维持真实产品的形态——真实的表经常同时有好几个低基数维度列——所以这里
 * 用 `within(screen.getByTestId('dimension-customer_state'))` 把查询限定
 * 在一个 dimension 块内部消歧义，而不是把 fixture 缩成只剩一列。
 *
 * 但要如实记一句：这份多维度 fixture 本身**不会**捕获 radio 的 `name`
 * 分组写错这类 bug（比如把 `name={\`dim-${name}\`}` 改成常量
 * `"dim"`，让 5 组单选被浏览器当成同一组）。实测过：这么改之后全部测试
 * 依旧全绿，因为测试里 `onDecisionChange` 是 `vi.fn()`，组件是不受控
 * 的——`decision` prop 不会因为点击而更新，点击不触发第二次渲染，
 * 分组串扰也就不会以 DOM `checked` 状态的形式暴露出来。这个缺口目前没
 * 有任何测试覆盖，见下面"点一列的 radio 不会带翻另一列的决定"那条测试
 * 旁边的说明。
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

/** demo 租户那张电商订单宽表，跟 draftProposal.test.ts 里的 demoColumns 同形状。 */
function demoStats(): ColumnStats[] {
  return [
    stat('订单号', 9998),
    stat('产品', 10, 'string', ['咖啡', '茶', '可乐']),
    stat('公司', 3, 'string', ['甲公司', '乙公司']),
    stat('类目', 4, 'string', ['饮料', '零食']),
    stat('用户名', 800),
    stat('revenue', 500, 'number'),
    stat('units_sold', 20, 'number'),
    stat('purchase_date', 300, 'date', ['2026-01-15']),
    stat('customer_state', 50, 'string', ['加州', '德州', '纽约州']),
    stat('internal_note', 6000),
  ]
}

/**
 * 纯维度表（产品主数据这类），没有标识列——root 是猜的。跟
 * draftProposal.test.ts 里的 noIdentifierColumns 同形状，专门用来触发
 * `proposal.rootIsGuessed`。
 */
function noIdentifierStats(): ColumnStats[] {
  return [
    stat('产品', 10, 'string', ['咖啡', '茶', '可乐']), // 猜测根
    stat('类目', 4, 'string', ['饮料', '零食']),
    stat('revenue', 500, 'number'),
    stat('purchase_date', 300, 'date', ['2026-01-15']),
  ]
}

const roled: RoledColumn[] = assignRoles(demoStats())
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
    // demo 里有 5 个 dimension 列，5 组单选都叫"建成实体"——查询必须限定
    // 在 customer_state 这一块内部，裸的 screen.findByRole 会因为多个
    // 匹配而报 "Found multiple elements"。
    const block = await screen.findByTestId('dimension-customer_state')
    const radio = within(block).getByRole('radio', { name: /建成实体/ })
    expect((radio as HTMLInputElement).checked).toBe(true)
  })

  it('显示判定依据里的具体数字', async () => {
    renderReview()
    expect((await screen.findByTestId('dimension-customer_state')).textContent).toMatch(/50/)
  })

  it('点「做成属性」会把该列标成非实体', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    renderReview({ onDecisionChange: onChange })
    const block = await screen.findByTestId('dimension-customer_state')
    await user.click(within(block).getByRole('radio', { name: /做成属性/ }))
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({
        dimensionsAsEntity: expect.objectContaining({ customer_state: false }),
      }),
    )
  })

  it('点一列的 radio 不会带翻另一列的决定', async () => {
    // 这条实际守住的是 setDecision 的 spread 完整性：改一列的决定时，
    // 传给 onDecisionChange 的新对象不会把别的列的决定弄丢。
    //
    // 它守不住的是 radio 的 name 分组——name 必须按列拼
    // （name={`dim-${name}`}），写成常量会让 5 组单选被浏览器当成同一
    // 组，选中一列会连带取消另一列。这个缺口目前没有任何测试覆盖：
    // 组件在这里是不受控的（onDecisionChange 是 mock，decision prop 不
    // 会因为点击回流），点击不会触发第二次渲染，分组串扰也就不会以 DOM
    // checked 状态的形式暴露出来。
    const user = userEvent.setup()
    const onChange = vi.fn()
    renderReview({ onDecisionChange: onChange })
    const block = await screen.findByTestId('dimension-customer_state')
    await user.click(within(block).getByRole('radio', { name: /做成属性/ }))
    expect(onChange).toHaveBeenCalled()
    const next = onChange.mock.calls[onChange.mock.calls.length - 1][0] as GuidedDecision
    expect(next.dimensionsAsEntity['产品']).toBe(true)
    expect(next.dimensionsAsEntity['类目']).toBe(true)
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

describe('字段名被清洗过的列', () => {
  it('renamedFields 非空时，原列名到清洗后字段名的对应关系要显示出来', async () => {
    // sanitizeFieldName 对纯中文列名会兜底成 field_1 这种占位名。用户
    // 下载 ETL 配置后会在 YAML 里看到自己从没在界面上见过的字段名——
    // 数据没丢，但改动对用户不可见，是"静默失败"的典型形态。
    renderReview({ proposal: { ...baseProposal, renamedFields: { 类目: 'field_1' } } })
    const block = await screen.findByTestId('renamed-fields')
    expect(block.textContent).toMatch(/类目/)
    expect(block.textContent).toMatch(/field_1/)
  })

  it('renamedFields 为空时不显示这个小节', async () => {
    renderReview({ proposal: { ...baseProposal, renamedFields: {} } })
    expect(screen.queryByTestId('renamed-fields')).toBeNull()
  })
})

describe('根节点是猜测的', () => {
  it('没有标识列时提示根是猜的', async () => {
    const guessedRoled = assignRoles(noIdentifierStats())
    const guessedDecision = initialDecision(guessedRoled)
    const guessedProposal = buildProposal(guessedRoled, guessedDecision)
    renderReview({ roled: guessedRoled, decision: guessedDecision, proposal: guessedProposal })
    expect(await screen.findByRole('alert')).toBeTruthy()
  })

  it('有标识列时不提示根是猜的', async () => {
    renderReview()
    // 先等页面渲染完成，再断言 alert 不存在——不然"还没渲染出来"和
    // "渲染了但没有 alert"这两种情况会被误判成同一个结果。
    await screen.findByTestId('dimension-customer_state')
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
