import { describe, expect, it } from 'vitest'
import * as XLSX from 'xlsx'
import {
  DISTINCT_LIMIT,
  MAX_XLSX_BYTES,
  TEXT_CHUNK_BYTES,
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

describe('isWholeNumber：这一列原本是不是无小数的数值', () => {
  it('高基数整数列被改判成 string 之后，这一位仍然是 true', () => {
    // inferredType 已经答不出这个问题了（高基数无小数数值列一律改判成
    // 'string'），而 columnRoles 的整数专属判定和文案要靠这一位——现实里的
    // 数量/单价/以分为单位的金额几乎都在 50 个不同值以上。
    const rows = Array.from({ length: 200 }, (_, i) => [`${100000 + i}`])
    const column = byName(['order_id'], rows).order_id
    expect(column.inferredType).toBe('string')
    expect(column.isWholeNumber).toBe(true)
  })

  it('带小数的数值列不是整数列', () => {
    expect(byName(['n'], [['1.5'], ['2']]).n.isWholeNumber).toBe(false)
  })

  it('混进一个非数字就不是整数列', () => {
    expect(byName(['n'], [['1'], ['2'], ['N/A']]).n.isWholeNumber).toBe(false)
  })

  it('全空的列不是整数列', () => {
    // 一个值都没见过时说"它全是整数"是空口断言，下游会据此走整数分支。
    expect(byName(['n'], [[''], ['']]).n.isWholeNumber).toBe(false)
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

  it('xlsx 里真实的日期列被识别成 date，不是数值序列号', async () => {
    // SheetJS 默认（不传 cellDates: true）把日期单元格读成 Excel 内部的
    // 浮点序列号（比如 45678），不是 JS Date——那样 DATE_PATTERN 匹配不上，
    // 日期列会被静默判成 integer/string。这条测试直接构造一个带真实
    // Date 单元格的 xlsx，走 scanTableFile 的真实读取路径断言最终类型，
    // 不满足于走查代码。
    const sheet = XLSX.utils.aoa_to_sheet([
      ['order_date', 'amount', 'note'],
      [new Date(2026, 0, 15), 100, 'a'],
      [new Date(2026, 1, 3), 200, 'b'],
      [new Date(2026, 2, 20), 300, 'c'],
    ])
    const workbook = XLSX.utils.book_new()
    XLSX.utils.book_append_sheet(workbook, sheet, 'Sheet1')
    const buffer = XLSX.write(workbook, { type: 'array', bookType: 'xlsx' }) as ArrayBuffer
    const file = new File([buffer], 'orders.xlsx', {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    })
    const byName = Object.fromEntries((await scanTableFile(file)).map((s) => [s.name, s]))
    expect(byName.order_date.inferredType).toBe('date')
    expect(byName.amount.inferredType).toBe('integer')
  })
})

describe('CSV 分块读取', () => {
  it('跨多个分块的大文件，行数与基数不因切块而失真', async () => {
    // Step 8 原有的三个测试都远小于 TEXT_CHUNK_BYTES，从没真正触发过第二
    // 次循环。一行被切成两半、两半各自当成一行统计，会静默污染基数——这类
    // bug 不报错，必须专门造一个跨块的大文件才能测到。
    const states = ['CA', 'TX', 'NY', 'FL', 'WA']
    const rowCount = 200_000
    const lines = ['state,idx']
    for (let i = 0; i < rowCount; i++) {
      lines.push(`${states[i % states.length]},${i}`)
    }
    const csv = lines.join('\n') + '\n'
    expect(new TextEncoder().encode(csv).length).toBeGreaterThan(TEXT_CHUNK_BYTES * 1.5)

    const file = new File([csv], 'big.csv', { type: 'text/csv' })
    const byName = Object.fromEntries((await scanTableFile(file)).map((s) => [s.name, s]))
    expect(byName.state.nonEmptyCount).toBe(rowCount)
    expect(byName.state.distinctCount).toBe(states.length)
    expect(byName.idx.nonEmptyCount).toBe(rowCount)
  })

  it('多字节 UTF-8 字符正好切在块边界上，不产生乱码', async () => {
    // 精确控制前面内容的字节数，让"中"这个三字节字符的第一个字节正好是
    // 第一块的最后一个字节，后两个字节落进第二块。如果分块逻辑丢了跨块
    // 状态（比如每次循环重置 pending），这个字符会被拆散成乱码或替换字符。
    const header = 'id,text\n'
    const rowPrefix = '1,'
    const paddingLength = TEXT_CHUNK_BYTES - 1 - header.length - rowPrefix.length
    const padding = 'x'.repeat(paddingLength)
    const tail = '中AB\n'
    const csv = header + rowPrefix + padding + tail
    const expectedValue = padding + '中AB'

    // 校验边界确实卡在预期位置：padding 结束处正是 TEXT_CHUNK_BYTES - 1
    // 字节，"中"的第一个字节落在第一块的最后一个字节上。
    const prefixBytes = new TextEncoder().encode(header + rowPrefix + padding).length
    expect(prefixBytes).toBe(TEXT_CHUNK_BYTES - 1)

    const file = new File([csv], 'boundary.csv', { type: 'text/csv' })
    const byName = Object.fromEntries((await scanTableFile(file)).map((s) => [s.name, s]))
    expect(byName.text.nonEmptyCount).toBe(1)
    expect(byName.text.samples[0]).toBe(expectedValue)
    expect(byName.text.samples[0]).not.toContain('�')
  })
})
