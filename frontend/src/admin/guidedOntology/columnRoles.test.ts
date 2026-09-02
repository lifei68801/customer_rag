import { describe, expect, it } from 'vitest'
import { assignRoles } from './columnRoles'
import type { ColumnStats } from './types'

function stats(over: Partial<ColumnStats>): ColumnStats {
  return {
    name: 'c',
    nonEmptyCount: 1000,
    distinctCount: 50,
    distinctCapped: false,
    samples: [],
    inferredType: 'string',
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
