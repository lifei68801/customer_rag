import { describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ProposalReview } from './ProposalReview'
import { buildProposal, initialDecision } from './draftProposal'
import { assignRoles } from './columnRoles'
import { accumulateRow, createAccumulator, finalizeStats } from './columnStats'
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
  isWholeNumber = false,
): ColumnStats {
  return {
    name,
    nonEmptyCount: 10000,
    distinctCount,
    distinctCapped: false,
    samples,
    inferredType,
    isWholeNumber,
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

describe('被当成标识的列', () => {
  it('标识列连同判定依据一起展示出来，不是只在别人的「挂在」下拉框里露个名字', async () => {
    renderReview()
    const block = await screen.findByTestId('identifier-订单号')
    // 依据要带具体数字——用户要能据此推翻它。
    expect(block.textContent).toMatch(/9998/)
  })

  it('能把标识列改判成「做成属性」', async () => {
    // 判错成标识的代价最重：这一列会成为本体的中心，ETL 给每一个值建一个
    // 节点。没有这个入口，用户看得见也纠正不了。
    const user = userEvent.setup()
    const onChange = vi.fn()
    renderReview({ onDecisionChange: onChange })
    const block = await screen.findByTestId('identifier-订单号')
    await user.click(within(block).getByRole('radio', { name: /做成属性/ }))
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({
        dimensionsAsEntity: expect.objectContaining({ 订单号: false }),
      }),
    )
  })

  it('默认选中「建成实体」', async () => {
    renderReview()
    const block = await screen.findByTestId('identifier-订单号')
    const radio = within(block).getByRole('radio', { name: /建成实体/ }) as HTMLInputElement
    expect(radio.checked).toBe(true)
  })

  it('走真实扫描链路的高基数整数金额列，在界面上可见且可改判', async () => {
    // 这条走 columnStats → columnRoles → buildProposal → 界面的整条链路，
    // 复现复审实测出的那个 Critical：2000 行、每行一个不同值的「金额分」
    // 在扫描阶段 distinct 封顶，inferredType 被改判成 'string'，于是行数
    // 下限管不着它，columnRoles 判成 identifier，它直接成了本体的中心，
    // 而 rootIsGuessed 是 false——连"中心是猜的"那条告警都不出。
    // 阈值救不了这件事（一列金额和一列真订单号在分布上无法区分），
    // 唯一站得住的兜底是它在审阅视图里可见、可改判。
    const acc = createAccumulator(['金额分', '产品'])
    for (let i = 0; i < 2000; i += 1) {
      accumulateRow(acc, [String(100000 + i), ['咖啡', '茶', '可乐'][i % 3]])
    }
    const moneyRoled = assignRoles(finalizeStats(acc))
    const decision = initialDecision(moneyRoled)
    const proposal = buildProposal(moneyRoled, decision)
    // 前提：确实落进了那个 Critical 的形状，不然下面的断言测不出问题。
    expect(moneyRoled[0].stats.inferredType).toBe('string')
    expect(moneyRoled[0].role).toBe('identifier')
    expect(proposal.rootName).toBe('金额分')
    expect(proposal.rootIsGuessed).toBe(false)

    const onChange = vi.fn()
    const user = userEvent.setup()
    render(
      <ProposalReview
        roled={moneyRoled}
        decision={decision}
        onDecisionChange={onChange}
        proposal={proposal}
      />,
    )
    const block = await screen.findByTestId('identifier-金额分')
    await user.click(within(block).getByRole('radio', { name: /做成属性/ }))
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({
        dimensionsAsEntity: expect.objectContaining({ 金额分: false }),
      }),
    )
  })
})

describe('会成为属性的列', () => {
  it('度量列和日期列的去向可见，不是从界面上消失', async () => {
    // 这两类列此前在审阅视图里一处都不出现：一列本该建成实体的数值列被
    // 判成度量时，用户既看不见也无从纠正。
    renderReview()
    const block = await screen.findByTestId('attribute-columns')
    expect(block.textContent).toMatch(/revenue/)
    expect(block.textContent).toMatch(/purchase_date/)
  })

  it('说清它们挂在谁身上，以及真做得到的改法', async () => {
    renderReview()
    const block = await screen.findByTestId('attribute-columns')
    expect(block.textContent).toMatch(/订单号/)
    expect(block.textContent).toMatch(/本体结构/)
  })

  it('一个属性都没有时不显示这个小节', async () => {
    renderReview({ proposal: { ...baseProposal, attributeColumns: [] } })
    expect(screen.queryByTestId('attribute-columns')).toBeNull()
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

  it('文案不指向一个界面上不存在的「上一步」', async () => {
    // review 步骤没有回退控件——「回上一步换一张表」是一个做不到的承诺，
    // 用户会去找一个不存在的按钮，最后只能刷新页面重来。页面顶上真做得到
    // 的动作是「换一张表」（GuidedOntologyPage 的 handleStartOver，
    // guidedPage.test.tsx 里有一条测试钉住它）。
    renderReview()
    const unused = await screen.findByTestId('unused-columns')
    expect(unused.textContent).not.toMatch(/回上一步/)
    expect(unused.textContent).toMatch(/换一张表/)
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

/**
 * 中心实体消失后的界面。
 *
 * 修复前 ProposalReview 用「不在 decision.parentOf 里的实体」反推中心。
 * 用户把猜测根改判成属性后那个反推得到 undefined，于是
 * `entityNames.filter(name => name !== rootName)` 不再排除任何人：每个实体
 * 都长出一行「挂在」，下拉框的值（那个已消失的旧根）不在 options 里，DOM
 * 静默回落到第一个选项——界面显示「品牌 挂在 颜色」「颜色 挂在 品牌」，
 * 一个不存在的环，而提交出去是零条关系。
 *
 * 四个维度列，不是三个：只有两个实体的旧 fixture 里，「颜色」的下拉框
 * 排掉自己后 options 只剩「品牌」一项——不管值来自 constraints、来自
 * decision，还是干脆写死，结果都是「品牌」，测不出实现取错了数据源。
 * 加一个「尺寸」，让「颜色」有两个候选上级（品牌、尺寸），并且让正确
 * 答案（尺寸）不是 options 里的第一个（品牌是）。
 */
function fourDimensionStats(): ColumnStats[] {
  return [
    stat('产品类目', 10, 'string', ['饮料', '零食']), // 猜测根，之后被改判成属性
    stat('品牌', 8, 'string', ['甲', '乙']), // 顺延后的新中心
    stat('颜色', 5, 'string', ['红', '蓝']),
    stat('尺寸', 4, 'string', ['S', 'M', 'L']),
    stat('revenue', 500, 'number'),
  ]
}

function renderWithRootDropped() {
  const dropped = assignRoles(fourDimensionStats())
  const decision = initialDecision(dropped)
  decision.dimensionsAsEntity['产品类目'] = false
  // 颜色显式挂在「尺寸」下面——尺寸在 entityNames 里排第二（插入顺序是
  // 品牌、颜色、尺寸），排掉自己后颜色的 options 是 [品牌, 尺寸]，品牌
  // 排第一。正确答案（尺寸）不是第一个选项，DOM 回落测不出来。
  decision.parentOf['颜色'] = '尺寸'
  decision.relationNameOf['颜色'] = 'SIZED_AS'
  // 尺寸自己保持 initialDecision 给的默认值（旧根「产品类目」），产品类目
  // 被改判成属性之后这条 parentOf 就失效了——尺寸会被 buildProposal 改挂
  // 到新中心「品牌」下面，用于覆盖「明说哪些实体被改挂了」那条测试。
  const proposal = buildProposal(dropped, decision)

  // decision 留一份陈旧值：颜色的上级和关系名都被换成了别的东西，但
  // proposal 是用上面那份决策算出来的，不会跟着变——这就是
  // decision.parentOf 里"残留指向已经不对的条目"的样子。渲染时把这份
  // 陈旧 decision 传给组件：如果实现从 decision 取值而不是从
  // proposal.constraints 反查，界面会显示错的上级和错的关系名。
  const staleDecision: GuidedDecision = {
    ...decision,
    parentOf: { ...decision.parentOf, 颜色: '品牌' },
    relationNameOf: { ...decision.relationNameOf, 颜色: 'STALE_NAME' },
  }

  render(
    <ProposalReview
      roled={dropped}
      decision={staleDecision}
      onDecisionChange={vi.fn()}
      proposal={proposal}
    />,
  )
}

describe('猜测根被改判成属性之后', () => {
  it('只有非中心实体有「挂在」那一行，中心自己没有', async () => {
    renderWithRootDropped()
    // 品牌是顺延出来的新中心，它不该再出现一行「挂在」——出现了就意味着
    // 中心反推又回到了 undefined。
    expect(await screen.findByLabelText(/颜色 挂在/)).toBeTruthy()
    expect(screen.queryByLabelText(/品牌 挂在/)).toBeNull()
  })

  it('「挂在」显示的上级就是会被提交的那个，不是第一个选项，也不是 decision 里的陈旧值', async () => {
    renderWithRootDropped()
    const select = (await screen.findByLabelText(/颜色 挂在/)) as HTMLSelectElement
    // 前提：第一个选项确实是「品牌」——不然下面 select.value 断言就算凑
    // 巧读到了 rootName 也测不出问题。
    expect([...select.options].map((o) => o.value)[0]).toBe('品牌')
    expect(select.value).toBe('尺寸')
  })

  it('关系名输入框显示的也是会被提交的那个，不是 decision 里的陈旧值', async () => {
    renderWithRootDropped()
    const input = (await screen.findByLabelText('颜色 的关系名')) as HTMLInputElement
    expect(input.value).toBe('SIZED_AS')
  })

  it('明说哪些实体被改挂了，不是悄悄改的', async () => {
    renderWithRootDropped()
    // 这次被改挂的是「尺寸」（它的旧上级「产品类目」被改判成了属性），
    // 不是「颜色」——颜色这次有一个显式指定的有效上级（尺寸），不该被
    // 改挂。
    const notice = await screen.findByTestId('reparented-notice')
    expect(notice.textContent).toMatch(/尺寸/)
    expect(notice.textContent).toMatch(/品牌/)
  })

  it('提示里的中心名不是空的', async () => {
    // 修复前这里渲染成「」——一句读不通的话。
    renderWithRootDropped()
    // 精确挑出「根是猜的」那条提示（不是改挂提示——后者也含「品牌」，
    // 拿它兜底的话中心名渲染成空也测不出来）。
    const alerts = await screen.findAllByRole('alert')
    const guessedAlert = alerts.find((el) => el.textContent?.includes('这里换不了中心'))
    expect(guessedAlert?.textContent).toMatch(/现在拿「品牌」当中心/)
  })
})

describe('猜测根提示的文案只承诺界面做得到的事', () => {
  function guessedAlertText() {
    const guessedRoled = assignRoles(noIdentifierStats())
    const guessedDecision = initialDecision(guessedRoled)
    render(
      <ProposalReview
        roled={guessedRoled}
        decision={guessedDecision}
        onDecisionChange={vi.fn()}
        proposal={buildProposal(guessedRoled, guessedDecision)}
      />,
    )
    return screen.getByRole('alert').textContent ?? ''
  }

  it('不建议「回上一步换一张表」', () => {
    // 纯维度表本来就没有标识列，换任何一张同类的表结果都一样。
    expect(guessedAlertText()).not.toMatch(/换一张表/)
  })

  it('不声称下面的下拉框能换掉中心', () => {
    // 下拉框重挂的是**非中心**实体；中心自己没有那一行，用它换不了中心。
    // 文案必须明说这里换不了，并指向真做得到的动作（把中心那列改成属性）。
    const text = guessedAlertText()
    expect(text).toMatch(/这里换不了中心/)
    expect(text).toMatch(/做成属性/)
  })
})

describe('所有维度列都被改判成属性之后，一个实体都不剩', () => {
  function renderEmptyOntology() {
    const guessedRoled = assignRoles(noIdentifierStats())
    const guessedDecision = initialDecision(guessedRoled)
    // noIdentifierStats 里唯一的维度列是「产品」（猜测根）；把它也改判成
    // 属性，本体里就一个实体都不剩了。
    for (const column of guessedRoled) {
      if (column.role === 'dimension') guessedDecision.dimensionsAsEntity[column.stats.name] = false
    }
    const proposal = buildProposal(guessedRoled, guessedDecision)
    // 前提：确实触发了空中心，不然下面的断言在原理上测不出问题。
    expect(proposal.rootName).toBe('')
    expect(proposal.termTypes.length).toBe(0)
    render(
      <ProposalReview
        roled={guessedRoled}
        decision={guessedDecision}
        onDecisionChange={vi.fn()}
        proposal={proposal}
      />,
    )
  }

  it('提示不再渲染成「现在拿「」当中心」，而是说清本体是空的', async () => {
    // 修复前这条分支跟"根是猜的但还有中心"共用一句文案，渲染出
    // 「现在拿「」当中心，其余实体都挂在它下面」——中心名是空的、没有
    // 其余实体、"改成做成属性"正是用户刚做完的事，三处都不成立。
    renderEmptyOntology()
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).not.toMatch(/现在拿「」/)
    expect(alert.textContent).toMatch(/一个实体都没有|没有实体/)
    expect(alert.textContent).toMatch(/建成实体/)
  })
})

describe('一条关系都没有时', () => {
  // buildProposal 现在产不出这个组合（每个非中心实体都会拿到一条边），
  // 所以直接构造 Proposal 喂给渲染边界——它是 prop，这条守的是"未来任何
  // 让关系归零的改动，用户能看见"。
  const orphanProposal: Proposal = {
    termTypes: [
      { value: '品牌', extra_fields: [], standard_name_value_type: 'string' },
      { value: '颜色', extra_fields: [], standard_name_value_type: 'string' },
    ],
    relationTypes: [],
    constraints: [],
    unusedColumns: [],
    attributeColumns: [],
    renamedFields: {},
    rootIsGuessed: false,
    rootName: '品牌',
    reparentedTo: { root: '品牌', names: [] },
  }

  it('显式警告，而不是显示成一切正常', async () => {
    renderReview({ proposal: orphanProposal })
    const warning = await screen.findByTestId('no-relations-warning')
    expect(warning.textContent).toMatch(/一条关系都没有/)
  })

  it('有关系时不出现这条警告', async () => {
    renderReview()
    await screen.findByTestId('dimension-customer_state')
    expect(screen.queryByTestId('no-relations-warning')).toBeNull()
  })
})

describe('没有用到的列，各自的原因要能看见', () => {
  it('整数列因行数不够落进 freetext 时，界面显示的是这一列专属的真实原因', async () => {
    // 3 行整数、互不相同（ratio 1.0）：INTEGER_IDENTIFIER_MIN_ROWS(=20)
    // 不够，落进 freetext。通用那句"重复度既不足以当分类，也没高到每行
    // 一个"对这一列是假话——它恰恰高到每行一个，只是行数不够。
    const unitPriceStat: ColumnStats = {
      name: 'unit_price',
      nonEmptyCount: 3,
      distinctCount: 3,
      distinctCapped: false,
      samples: ['10', '20', '30'],
      inferredType: 'integer',
      isWholeNumber: true,
    }
    const roledWithUnitPrice = assignRoles([stat('订单号', 9998, 'string'), unitPriceStat])
    const decision = initialDecision(roledWithUnitPrice)
    const proposal = buildProposal(roledWithUnitPrice, decision)
    // 前提：这一列确实落进了未使用列表——不然下面断言测不出问题。
    expect(proposal.unusedColumns).toContain('unit_price')
    render(
      <ProposalReview
        roled={roledWithUnitPrice}
        decision={decision}
        onDecisionChange={vi.fn()}
        proposal={proposal}
      />,
    )
    const unused = await screen.findByTestId('unused-columns')
    expect(unused.textContent).toMatch(/unit_price/)
    // 专属原因（来自 columnRoles.ts 的整数文案）要出现在界面上。
    expect(unused.textContent).toMatch(/本体结构/)
    // 通用那句对这一列不成立的半句话不能再出现。
    expect(unused.textContent).not.toMatch(/也没高到每行一个/)
  })
})

describe('挂在下拉框没有对应 constraint 时', () => {
  // buildProposal 的不变量保证每个非中心实体都恰好有一条入边，正常路径
  // 走不到这里——这条测的是渲染边界本身：Proposal 是外部传入的 prop，
  // 任何打破那条不变量的未来改动，都不该让界面悄悄画出一条不会被提交
  // 的「X 挂在中心」的边（这轮 Critical 的形态）。
  const proposalMissingConstraint: Proposal = {
    termTypes: [
      { value: '品牌', extra_fields: [], standard_name_value_type: 'string' },
      { value: '颜色', extra_fields: [], standard_name_value_type: 'string' },
      { value: '尺寸', extra_fields: [], standard_name_value_type: 'string' },
    ],
    relationTypes: [],
    // 只有"颜色"有 constraint，"尺寸"没有——模拟它在 constraints 里缺席。
    constraints: [
      { subject_term_type: '品牌', relation_type: 'HAS_颜色', object_term_type: '颜色' },
    ],
    unusedColumns: [],
    attributeColumns: [],
    renamedFields: {},
    rootIsGuessed: false,
    rootName: '品牌',
    reparentedTo: { root: '品牌', names: [] },
  }

  it('不悄悄兜底成中心，显式渲染成「未连接」', async () => {
    renderReview({ proposal: proposalMissingConstraint })
    const select = (await screen.findByLabelText(/尺寸 挂在/)) as HTMLSelectElement
    expect(select.value).not.toBe('品牌')
    const optionLabels = [...select.options].map((o) => o.textContent)
    expect(optionLabels).toContain('未连接')
  })
})
