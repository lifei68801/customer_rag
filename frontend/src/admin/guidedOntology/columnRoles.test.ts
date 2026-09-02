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

const roleOf = (s: ColumnStats, totalRows = 1000) => assignRoles([s], totalRows)[0].role

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
    const [roled] = assignRoles([stats({ name: 'customer_state', distinctCount: 50 })], 1000)
    expect(roled.reason).toMatch(/50/)
  })
})

describe('空列', () => {
  it('整列为空不判成任何有意义的角色', () => {
    // 判成维度的话会建出一个没有任何实例的实体类型。
    const role = roleOf(stats({ name: 'unused', nonEmptyCount: 0, distinctCount: 0 }))
    expect(role).toBe('freetext')
  })
})
