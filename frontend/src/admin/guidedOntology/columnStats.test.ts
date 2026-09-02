import { describe, expect, it } from 'vitest'
import {
  DISTINCT_LIMIT,
  MAX_XLSX_BYTES,
  accumulateRow,
  createAccumulator,
  finalizeStats,
  scanTableFile,
} from './columnStats'

/** 造一份统计结果：列名 + 每行的值。 */
function statsOf(columns: string[], rows: string[][]) {
  const acc = createAccumulator(columns)
  for (const row of rows) accumulateRow(acc, row)
  return finalizeStats(acc)
}

const byName = (columns: string[], rows: string[][]) =>
  Object.fromEntries(statsOf(columns, rows).map((s) => [s.name, s]))

describe('基数统计', () => {
  it('数出不同值的个数', () => {
    const stats = byName(['state'], [['CA'], ['TX'], ['CA'], ['NY']])
    expect(stats.state.distinctCount).toBe(3)
    expect(stats.state.distinctCapped).toBe(false)
  })

  it('超过上限就封顶并打标，不再继续收集', () => {
    // 不封顶的话，一张百万行的表会把每列的所有值都留在内存里。判定只需要
    // 知道"低基数还是高基数"，不需要精确数字。
    const rows = Array.from({ length: DISTINCT_LIMIT + 500 }, (_, i) => [`v${i}`])
    const stats = byName(['id'], rows)
    expect(stats.id.distinctCapped).toBe(true)
    expect(stats.id.distinctCount).toBe(DISTINCT_LIMIT)
  })

  it('空值不算进非空计数，也不算进不同值', () => {
    // 把空值当成一个"值"的话，一列 90% 为空的数据会被算成低基数，
    // 判成实体类型——而它其实是个稀疏的可选字段。
    const stats = byName(['note'], [['a'], [''], ['b'], ['']])
    expect(stats.note.nonEmptyCount).toBe(2)
    expect(stats.note.distinctCount).toBe(2)
  })
})

describe('类型推断', () => {
  it('全是整数 → integer', () => {
    expect(byName(['n'], [['1'], ['2'], ['30']]).n.inferredType).toBe('integer')
  })

  it('有小数 → number', () => {
    expect(byName(['n'], [['1.5'], ['2']]).n.inferredType).toBe('number')
  })

  it('一个非数字就不是数值列', () => {
    // 混进一个 "N/A" 就整列当字符串——按数值处理会在 ETL 时静默丢掉那一行，
    // 或者把 N/A 变成 0。
    expect(byName(['n'], [['1'], ['2'], ['N/A']]).n.inferredType).toBe('string')
  })

  it('日期格式 → date', () => {
    expect(byName(['d'], [['2026-01-15'], ['2026-02-03']]).d.inferredType).toBe('date')
  })

  it('纯数字的订单号不该被当成数值列', () => {
    // 高基数的整数列几乎总是标识而不是度量。把它判成 number 会让它被
    // 归进属性，整个实体就没了。
    const rows = Array.from({ length: 200 }, (_, i) => [`${100000 + i}`])
    expect(byName(['order_id'], rows).order_id.inferredType).toBe('string')
  })
})

describe('样例值', () => {
  it('保留前几个不同值给用户看', () => {
    // 判断"这列该不该是实体"时，用户要看到真实的值。只给一个数字
    // （"50 个不同值"）他判断不了。
    const stats = byName(['state'], [['CA'], ['TX'], ['CA'], ['NY'], ['FL']])
    expect(stats.state.samples.slice(0, 3)).toEqual(['CA', 'TX', 'NY'])
  })
})

describe('列数与行长不一致', () => {
  it('短行按空值补齐，不报错', () => {
    // CSV 里尾部空列常被省略。报错会让整个引导卡在第一步。
    const stats = byName(['a', 'b'], [['1', '2'], ['3']])
    expect(stats.b.nonEmptyCount).toBe(1)
  })
})

describe('scanTableFile', () => {
  it('读 CSV，表头之外的每一行都算进去', async () => {
    const csv = 'state,revenue\nCA,10.5\nTX,20\nCA,30\n'
    const file = new File([csv], 'orders.csv', { type: 'text/csv' })
    const stats = await scanTableFile(file)
    const byName = Object.fromEntries(stats.map((s) => [s.name, s]))
    expect(byName.state.distinctCount).toBe(2)
    expect(byName.state.nonEmptyCount).toBe(3)
    expect(byName.revenue.inferredType).toBe('number')
  })

  it('带引号的字段按 CSV 规则解析', async () => {
    const csv = 'name,note\n"A,B","says ""hi"""\n'
    const file = new File([csv], 'x.csv', { type: 'text/csv' })
    const byName = Object.fromEntries((await scanTableFile(file)).map((s) => [s.name, s]))
    expect(byName.name.samples).toEqual(['A,B'])
  })

  it('超大 xlsx 明确拒绝，不是静静卡住', async () => {
    // xlsx 必须整个读进内存。页面卡死时用户不知道发生了什么，也不知道
    // 该怎么办；一条明确的错误信息至少告诉他换个小一点的文件。
    const big = new File([new Uint8Array(MAX_XLSX_BYTES + 1)], 'big.xlsx')
    await expect(scanTableFile(big)).rejects.toThrow(/过大|太大/)
  })
})
