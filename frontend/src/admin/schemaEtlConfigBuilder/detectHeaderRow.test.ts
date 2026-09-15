import { describe, expect, it } from 'vitest'
import { detectHeaderRow } from './detectHeaderRow'

/**
 * 造一行宽度为 width、前 filled 个格子非空的行。detectHeaderRow 只统计非空
 * 格子数，不关心哪一列非空，所以这样构造跟真实文件里非空格子散落各处是等
 * 价的——但能精确还原任意非空比例，不用手敲上百个字符串字面量。
 */
function makeRow(filled: number, width: number): string[] {
  return Array.from({ length: width }, (_, i) => (i < filled ? 'v' : ''))
}

describe('detectHeaderRow', () => {
  it('普通的表：第一行就是表头', () => {
    const rows = [
      ['name', 'code'],
      ['foo', 'A1'],
      ['bar', 'A2'],
    ]

    expect(detectHeaderRow(rows)).toBe(1)
  })

  it('MUJI 的形状：合并标题带在第一行，真表头在下面', () => {
    // 这组非空格子数是从真实文件（CN_001_SKU_MASTER_121.xls，Master 工作表，
    // 113 列）里数出来的第 1~7 行，不是估的。用 makeRow 按原比例（113 列）
    // 还原，不缩放——缩放会让 107 跟 108 这种相近的数字被四舍五入到同一个
    // 值，丢失真实的落差。
    //   第 1 行：5   —— 合并标题带
    //   第 2 行：8   —— 几乎全空
    //   第 3 行：107 —— 英文列名
    //   第 4 行：92  —— 日文列名
    //   第 5 行：105 —— Character Limit 说明行
    //   第 6 行：108 —— 系统代码列名，真表头
    //   第 7 行起：72（各行一致）—— 数据
    const WIDTH = 113
    const rows = [
      makeRow(5, WIDTH),
      makeRow(8, WIDTH),
      makeRow(107, WIDTH),
      makeRow(92, WIDTH),
      makeRow(105, WIDTH),
      makeRow(108, WIDTH),
      makeRow(72, WIDTH),
      makeRow(72, WIDTH),
      makeRow(72, WIDTH),
      makeRow(72, WIDTH),
      makeRow(72, WIDTH),
      makeRow(72, WIDTH),
    ]

    expect(detectHeaderRow(rows)).toBe(6)
  })

  it('顶上的说明块比真表头还满，但它下面没有数据', () => {
    // 这条用例锁住"自己非空"这个前提：第 1 行本身非空格子最多，但它跟表头
    // 之间隔着全空行，往下看不稳定（落差 1.0），会被跳过；表头下面是连续
    // 同密度的数据（落差 0），才会被选中。
    const rows = [
      ['注意', '本表仅供内部使用', '如有疑问请联系', '数据部', '2026'],
      ['', '', '', '', ''],
      ['', '', '', '', ''],
      ['', '', '', '', ''],
      ['', '', '', '', ''],
      ['jan', 'color', 'size', '', ''],
      ['4934761229522', 'Natural', 'S', '', ''],
      ['4934761229539', 'Natural', 'M', '', ''],
      ['4934761229546', 'Natural', 'L', '', ''],
      ['4934761229645', 'Black', 'S', '', ''],
      ['4934761229652', 'Black', 'M', '', ''],
    ]

    expect(detectHeaderRow(rows)).toBe(6)
  })

  it('数据本身比表头更满，依然认表头', () => {
    // 反例：表头有空列名（第三列没填），数据行反而每列都填了。按"谁更满"
    // 打分会被数据行反超；detectHeaderRow 用"从上往下，第一个满足条件就是
    // 表头"，一开始就锁定表头，不会因为后面的数据行更满就被抢走。
    const rows = [
      ['a', 'b', ''],
      ['1', '2', '3'],
      ['4', '5', '6'],
      ['7', '8', '9'],
    ]

    expect(detectHeaderRow(rows)).toBe(1)
  })

  it('候选行后面没有行可验证稳不稳定时，保守回退第一行', () => {
    // 表格很短，唯一看起来像表头的一整行（第 3 行）恰好是全表最后一行，
    // 没有后续行能验证"下面是不是稳定的数据"。这时不该因为"没有反例"就
    // 顺水推舟认下它——保守回退到第一行，交给用户自己确认。
    const rows = [
      ['a', 'b', 'c'],
      ['', '', ''],
      ['e', 'f', 'g'],
    ]

    expect(detectHeaderRow(rows)).toBe(1)
  })

  it('只有一行时返回第一行', () => {
    expect(detectHeaderRow([['a', 'b']])).toBe(1)
  })

  it('空表返回第一行', () => {
    expect(detectHeaderRow([])).toBe(1)
  })

  it('并列时取最靠上的一行', () => {
    // 第 2 行自己往下看（只剩第 3 行）也符合"稳定"条件，但 detectHeaderRow
    // 从上往下扫，第一个满足条件的是第 1 行，直接返回，不会继续找"更好"的。
    const rows = [
      ['a', 'b'],
      ['1', '2'],
      ['3', '4'],
    ]

    expect(detectHeaderRow(rows)).toBe(1)
  })
})
