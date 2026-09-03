import { describe, expect, it } from 'vitest'
import { assignRoles } from './columnRoles'
import { accumulateRow, createAccumulator, finalizeStats } from './columnStats'
import type { ColumnStats } from './types'

function stats(over: Partial<ColumnStats>): ColumnStats {
  return {
    name: 'c',
    nonEmptyCount: 1000,
    distinctCount: 50,
    distinctCapped: false,
    samples: [],
    inferredType: 'string',
    // 手写的统计量默认"不是整数列"。整数相关的用例一律走下面的 realStats，
    // 用真实扫描函数产出这一位，免得手写值和 inferType 的结论互相矛盾。
    isWholeNumber: false,
    ...over,
  }
}

const roleOf = (s: ColumnStats) => assignRoles([s])[0].role

describe('自动判定，不需要问用户', () => {
  it('数值列是度量', () => {
    expect(roleOf(stats({ name: 'revenue', inferredType: 'number' }))).toBe('measure')
  })

  it('几乎每行一个值的字符串列是标识', () => {
    expect(roleOf(stats({ name: '订单号', distinctCount: 998, nonEmptyCount: 1000 }))).toBe(
      'identifier',
    )
  })

  it('封顶的字符串列也是标识——封顶意味着至少 1000 个不同值', () => {
    expect(
      roleOf(stats({ name: 'id', distinctCount: 1000, distinctCapped: true, nonEmptyCount: 1000 })),
    ).toBe('identifier')
  })

  it('封顶且非空行数远大于封顶值，仍是标识而不是维度', () => {
    // 封顶意味着"至少 1000 个不同值"，不是"正好 1000 个"。5000 行里封顶
    // 的话真实基数可能就是 5000——那是标识。只看 ratio(=0.2) 会把它判成
    // 维度，于是引导会给它建一个有几千个节点的实体类型。
    expect(
      roleOf(stats({ nonEmptyCount: 5000, distinctCount: 1000, distinctCapped: true })),
    ).toBe('identifier')
  })

  it('日期列单独成一类', () => {
    // 不能混进 measure：数据模型没有日期类型，这一类要单独提示限制。
    expect(roleOf(stats({ name: 'purchase_date', inferredType: 'date' }))).toBe('date')
  })

  it('低基数字符串列是维度候选', () => {
    expect(roleOf(stats({ name: 'customer_state', distinctCount: 50 }))).toBe('dimension')
  })

  it('中等基数的字符串列是自由文本，不是维度', () => {
    // 一列有 600 个不同值（占 60%），建成实体类型会造出 600 个节点，
    // 而它们之间没有任何共性——那是备注，不是维度。
    expect(roleOf(stats({ name: 'note', distinctCount: 600, nonEmptyCount: 1000 }))).toBe('freetext')
  })
})

describe('每条判定都要带依据', () => {
  it('依据里有具体数字，不是一句空话', () => {
    // 用户要能据此推翻判定。"这是维度"没法推翻，"1000 行里 50 个不同值"
    // 可以——他知道自己的业务里州就是 50 个。
    const [roled] = assignRoles([stats({ name: 'customer_state', distinctCount: 50 })])
    expect(roled.reason).toMatch(/50/)
  })
})

describe('空列', () => {
  it('整列为空不判成任何有意义的角色', () => {
    // 判成维度的话会建出一个没有任何实例的实体类型。
    const role = roleOf(stats({ name: 'unused', nonEmptyCount: 0, distinctCount: 0 }))
    expect(role).toBe('freetext')
  })

  it('整列为空的 reason 要能说明原因', () => {
    // 光断言 role 不够：0/0 是 NaN，删掉空列守卫后两个阈值比较都会失败，
    // 恰好也落到 freetext 分支，role 断言测不出守卫被删。
    // 注意：不能用宽松的 /空/ ——fallback 分支的文案里有"非空值"，
    // 同样会匹配 /空/，测不出守卫被删。要匹配守卫专属的措辞。
    expect(
      assignRoles([stats({ nonEmptyCount: 0, distinctCount: 0 })])[0].reason,
    ).toMatch(/全是空的/)
  })

  it('非空行数为 0 但 distinctCount 非 0 时，不能被误判成标识', () => {
    // assignRoles 是消费任意 ColumnStats[] 的公开接口。没有守卫的话
    // ratio = 5/0 = Infinity，Infinity >= IDENTIFIER_MIN_RATIO 为真，这个
    // 空列会被判成"这一行的标识"——引导会把整个建模建在一根不存在的柱子上。
    expect(roleOf(stats({ nonEmptyCount: 0, distinctCount: 5 }))).not.toBe('identifier')
  })
})

/**
 * 数值列 × 各种比例。
 *
 * 这一格此前是空的：唯一的数值用例是 `revenue / number / distinct 50 /
 * nonEmpty 1000`（ratio 0.05），无论"数值短路"排在比例判定之前还是之后，
 * 结果都是 measure——那条测试从原理上就无法区分分支顺序的对错，于是
 * "integer 列永远走不到 identifier/dimension 判定"这个缺陷一直没被抓到。
 *
 * 下面的用例用真实的扫描函数（createAccumulator/accumulateRow/
 * finalizeStats）造统计量，而不是手写 ColumnStats——因为判定结果同时取决
 * 于 columnStats.ts 的 NUMERIC_IDENTIFIER_THRESHOLD(=50)（它在扫描阶段就
 * 把高基数整数列改判成 'string'）和 columnRoles.ts 的分支顺序，只测其中
 * 一半会漏掉两者的接缝。
 */
function realStats(name: string, values: string[]): ColumnStats {
  const acc = createAccumulator([name])
  for (const value of values) accumulateRow(acc, [value])
  return finalizeStats(acc)[0]
}

const range = (start: number, count: number) =>
  Array.from({ length: count }, (_, i) => String(start + i))

describe('数值列按分布判定，不是一律当度量', () => {
  it('200 行、1001–1200 的订单号是标识（高基数整数在扫描阶段已改判成字符串）', () => {
    const column = realStats('订单号', range(1001, 200))
    // 先确认前提成立：distinct 200 > 50，扫描阶段就不再是 integer。
    expect(column.inferredType).toBe('string')
    expect(roleOf(column)).toBe('identifier')
  })

  it('45 行、1–45 的订单号也是标识，不是度量', () => {
    // distinct 45 ≤ 50，扫描阶段留在 integer，只能靠比例判定救它。
    // 旧实现在这里返回 measure：小表的数值主键被静默吞掉，根就没了。
    const column = realStats('订单号', range(1, 45))
    expect(column.inferredType).toBe('integer')
    expect(roleOf(column)).toBe('identifier')
  })

  it('200 行、20 种取值的门店编号是维度，不是度量', () => {
    // 数字编码的低基数分类列（门店编号、区域码、状态码、类目 ID）在真实
    // 业务数据里极常见，而且基数不随行数增长——表再大也不会自己变对。
    // 判成 measure 的话它在审阅视图里根本不出现，用户无从纠正。
    const column = realStats(
      '门店编号',
      Array.from({ length: 200 }, (_, i) => String(101 + (i % 20))),
    )
    expect(column.inferredType).toBe('integer')
    expect(roleOf(column)).toBe('dimension')
  })

  it('3 行、全不重复的小数金额仍是度量', () => {
    // number（带小数）保留"数值即度量"的短路：ratio 1.0 也不该把它判成
    // 标识。这条守的是修复的边界——只有 integer 改走比例判定。
    const column = realStats('revenue', ['10.5', '20.25', '30.75'])
    expect(column.inferredType).toBe('number')
    expect(roleOf(column)).toBe('measure')
  })

  it('整数列的重复度落在两条阈值之间时是自由文本', () => {
    // 60 个非空值、30 个不同值：ratio 0.5，既够不上"几乎每行一个"，也
    // 谈不上"重复度高"。这条和上面两条一起，把 integer 的三条出口都钉住
    // ——只断言 identifier/dimension 的话，一个"integer 一律 dimension"
    // 的错误实现也能通过。
    const column = realStats(
      'ticket_no',
      Array.from({ length: 60 }, (_, i) => String(1 + (i % 30))),
    )
    expect(column.inferredType).toBe('integer')
    expect(column.distinctCount / column.nonEmptyCount).toBe(0.5)
    expect(roleOf(column)).toBe('freetext')
  })

  it('整数列的判定依据带上具体基数，用户能据此推翻', () => {
    const column = realStats(
      '门店编号',
      Array.from({ length: 200 }, (_, i) => String(101 + (i % 20))),
    )
    // 不用宽泛正则：/20/ 会被 200 里的 "20" 撞上，一个只会输出行数的
    // 错误实现也能过。要求两个数字都按语序出现。
    expect(assignRoles([column])[0].reason).toMatch(/200 个非空值里只有 20 个不同值/)
  })
})

describe('整数列判成标识需要够多的行', () => {
  it('3 行整数全不重复，不足以当标识', () => {
    // ratio 1.0 但只有 3 行——那不是"每行一个值"的证据，只是样本太小。
    // 判成 identifier 的后果最重：这一列会直接成为本体的中心，而审阅视图
    // 里根本不展示标识列，用户看不见也推不翻。
    const column = realStats('revenue', ['10', '20', '30'])
    expect(column.inferredType).toBe('integer')
    expect(roleOf(column)).not.toBe('identifier')
  })

  it('3 行整数落进自由文本，会出现在「没有用到的列」里', () => {
    // 只断言 not identifier 不够：判成 measure 同样能通过，而 measure 在
    // 审阅视图里不展示——那正是这一轮要消灭的静默形态。必须钉住它落到
    // 可见的那条出口。
    expect(roleOf(realStats('revenue', ['10', '20', '30']))).toBe('freetext')
  })

  it('20 行整数全不重复就是标识', () => {
    // 钉住门槛的位置。少了这条，一个"整数永远不能当标识"的实现也能让
    // 上面两条通过，而那会让小表里真正的数值主键彻底消失。
    const column = realStats('订单号', range(1, 20))
    expect(column.inferredType).toBe('integer')
    expect(roleOf(column)).toBe('identifier')
  })

  it('19 行整数全不重复还不算标识——门槛就在 20', () => {
    expect(roleOf(realStats('订单号', range(1, 19)))).not.toBe('identifier')
  })

  it('字符串标识不受行数下限影响', () => {
    // 下限只卡整数。字符串标识（ORD1001 这种）没有"其实是度量"的另一种
    // 可能，卡它只会误伤小表——10 行的样例表也该认出它的主键。
    const column = realStats('订单号', ['ORD1', 'ORD2', 'ORD3'])
    expect(column.inferredType).toBe('string')
    expect(roleOf(column)).toBe('identifier')
  })
})

describe('整数列落进自由文本时的说明', () => {
  it('说清整数为什么没被当成度量，并给一个真做得到的动作', () => {
    // 通用那句「重复度不足以当分类」对一列整元单价是指错方向：那一列的
    // 问题不是"不够格当分类"，是系统没认出它是金额。
    const reason = assignRoles([realStats('unit_price', ['10', '20', '30'])])[0].reason
    expect(reason).toMatch(/只有带小数的数值列才会/)
    expect(reason).toMatch(/「本体结构」页/)
  })

  it('不同值超过 50 的整数列（数量、单价、以分为单位的金额）也拿得到这段说明', () => {
    // 500 行、120 个不同值：扫描阶段 NUMERIC_IDENTIFIER_THRESHOLD 已经把
    // inferredType 改判成 'string'。判"是不是整数列"只看 inferredType 的
    // 实现会给它字符串列的通用文案——而现实里的数量/单价/以分为单位的金额
    // 几乎都在 50 个不同值以上，整数专属文案对真正需要它的列一句都不出现。
    const column = realStats(
      'units_sold',
      Array.from({ length: 500 }, (_, i) => String(1 + (i % 120))),
    )
    // 前提：确实落在被改判的那一半里，而且比例既够不上标识也够不上维度。
    expect(column.inferredType).toBe('string')
    expect(column.isWholeNumber).toBe(true)
    expect(roleOf(column)).toBe('freetext')
    const reason = assignRoles([column])[0].reason
    expect(reason).toMatch(/只有带小数的数值列才会/)
    expect(reason).toMatch(/「本体结构」页/)
  })

  it('字符串列仍用原来的说明，没有被整数文案顶掉', () => {
    const reason = assignRoles([stats({ name: 'note', distinctCount: 600, nonEmptyCount: 1000 })])[0]
      .reason
    expect(reason).toMatch(/重复度不足以当分类/)
    expect(reason).not.toMatch(/「本体结构」页/)
  })
})
