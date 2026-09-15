import { describe, expect, it } from 'vitest'
import { detectHeaderRow } from './detectHeaderRow'

/**
 * 造一行宽度为 width、前 filled 个格子非空的行。detectHeaderRow 只统计非空
 * 格子数，不关心哪一列非空，所以这样构造跟真实文件里非空格子散落各处是等
 * 价的——但能精确还原任意非空格子数，不用手敲上百个字符串字面量。
 */
function makeRow(filled: number, width: number): string[] {
  return Array.from({ length: width }, (_, i) => (i < filled ? 'v' : ''))
}

/**
 * 真实 MUJI 文件（CN_001_SKU_MASTER_121.xls，Master 工作表，113 列）第 1~7
 * 行的非空格子数，是数出来的，不是估的；第 8~14 行照第 7 行的数据行补齐。
 *   第 1 行：5   —— 合并标题带
 *   第 2 行：8   —— 几乎全空
 *   第 3 行：107 —— 英文列名
 *   第 4 行：92  —— 日文列名
 *   第 5 行：105 —— Character Limit 说明行
 *   第 6 行：108 —— 系统代码列名，真表头（比数据更满）
 *   第 7 行起：72 —— 数据
 */
const MUJI_COUNTS = [5, 8, 107, 92, 105, 108, 72, 72, 72, 72, 72, 72, 72, 72]
const MUJI_WIDTH = 113

function mujiRows(): string[][] {
  return MUJI_COUNTS.map((filled) => makeRow(filled, MUJI_WIDTH))
}

/** 在 rows[rowIndex] 的第 column 列（1-based）塞一个孤立的非空单元格，中间补空格子。 */
function withStrayCell(rows: string[][], rowIndex: number, column: number): string[][] {
  const copy = rows.map((row) => row.slice())
  const row = copy[rowIndex]
  while (row.length < column - 1) row.push('')
  row.push('备注')
  return copy
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
    // 数据块的起点先落在第 7 行（从第 7 行起连续 7 行都是 72 格，落差 0）；
    // 从第 3~6 行起的窗口都跨着表头层和数据，落差 36，超过 0.2 × 108 =
    // 21.6。第 6 行本身非空、且格子数（108）不少于数据块起点（72），往前
    // 挪一行，落到真表头第 6 行。按原比例（113 列）还原，不缩放——缩放会让
    // 107 跟 108 这种相近的数字被四舍五入到同一个值。
    expect(detectHeaderRow(mujiRows())).toBe(6)
  })

  it('顶上的说明块比真表头还满，但它下面没有数据', () => {
    // 第 1 行本身非空格子最多，但从它开始的窗口要跨过 4 行全空行才碰到表
    // 头，落差很大，测不出"稳定"；第 2~5 行全空，不能当数据块起点。表头
    // （第 6 行）的格子数跟后面的数据完全一致（都是 3 格），数据块直接从
    // 表头这一行开始，不需要"往前挪"这一步。
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
    // 反例：表头有空列名（第三列没填），数据行反而每列都填了。表头 2 格、
    // 数据 3 格，落差 1 格，刚好在"容忍 1 个格子"以内，表头和数据本来就
    // 在同一个稳定窗口里，数据块起点直接落在表头这一行。
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
    // 窗口长度只有 1（MIN_RUN_CONFIRM 要求至少 2 行才采信）。不能因为"没
    // 有反例"就顺水推舟认下它——保守回退到第一行，交给用户自己确认。
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
    // 表头和数据格子数完全一致，数据块的起点直接落在第 1 行，扫到即返回，
    // 不会因为第 2 行往下看也一样稳定就继续找"更晚"的一行。
    const rows = [
      ['a', 'b'],
      ['1', '2'],
      ['3', '4'],
    ]

    expect(detectHeaderRow(rows)).toBe(1)
  })

  it('表头正上方还有一行说明/标题，不能认说明行', () => {
    // 用 5 列而不是 2 列构造：2 列表下，"说明行只填了 1 格"跟"数据行缺了
    // 一格"（见下面的窄表用例）是同一个数字，分不出来——这是本函数的已知
    // 局限，写在 detectHeaderRow 的文档注释里了。5 列时说明行 1 格、表头/
    // 数据 5 格，落差 4 格，明显超出容差，能被正确排除。
    const rows = [
      ['Title', '', '', '', ''],
      ['name', 'code', 'age', 'city', 'country'],
      ['foo', 'A1', '1', 'bj', 'cn'],
      ['bar', 'A2', '2', 'sh', 'cn'],
      ['baz', 'A3', '3', 'gz', 'cn'],
      ['qux', 'A4', '4', 'sz', 'cn'],
      ['quux', 'A5', '5', 'hz', 'cn'],
    ]

    expect(detectHeaderRow(rows)).toBe(2)
  })

  it('表头前有一叠密度相近的垃圾行（信头/落款重复），不能认垃圾行', () => {
    // 5 行垃圾行都是 5 格，表头和数据都是 10 格——如果确认窗口跟垃圾行一
    // 样长（5 行），垃圾行自己就会先被误判成"稳定的数据块"。RUN_WINDOW_ROWS
    // 取 7（比垃圾行多 2 行）就是为了盖过这种情况：从垃圾行内部任何一行开
    // 始的 7 行窗口，都会探到垃圾行外面格子数不同的表头/数据，跳出稳定范围。
    const rows = [
      makeRow(5, 10),
      makeRow(5, 10),
      makeRow(5, 10),
      makeRow(5, 10),
      makeRow(5, 10),
      makeRow(10, 10),
      makeRow(10, 10),
      makeRow(10, 10),
      makeRow(10, 10),
      makeRow(10, 10),
      makeRow(10, 10),
      makeRow(10, 10),
      makeRow(10, 10),
      makeRow(10, 10),
    ]

    expect(detectHeaderRow(rows)).toBe(6)
  })

  it('数据本身参差不齐（可选字段忽多忽少），表头仍在第 1 行', () => {
    // 表头满格（10 格），数据行因为可选字段时有时无，在 8~10 格之间波动
    // ——落差 2 格，恰好等于 0.2 × 10，没有超出稳定容差，表头和数据从第 1
    // 行起就在同一个稳定窗口里，不需要"往前挪"。
    const rows = [
      makeRow(10, 10),
      makeRow(8, 10),
      makeRow(10, 10),
      makeRow(9, 10),
      makeRow(8, 10),
      makeRow(10, 10),
      makeRow(9, 10),
      makeRow(8, 10),
      makeRow(10, 10),
      makeRow(9, 10),
      makeRow(8, 10),
    ]

    expect(detectHeaderRow(rows)).toBe(1)
  })

  it('窄表：6 行数据里 1 个格子缺失，仍认第 1 行为表头', () => {
    // 2 列表，缺 1 格就是 50% 的比例落差，按比例的容差（0.2 × 2 = 0.4 格）
    // 对窄表天然不成立——这正是 TOLERANCE_CELLS 存在的原因：无论多窄，都
    // 容忍 1 个格子的有无。
    const rows = [
      ['a', 'b'],
      ['1', '2'],
      ['3', ''],
      ['4', '5'],
      ['6', '7'],
      ['8', '9'],
      ['10', '11'],
    ]

    expect(detectHeaderRow(rows)).toBe(1)
  })

  it('参差行宽（真实 SheetJS 形状）不影响判定：跟等宽版本结果一致', () => {
    // 真实文件经 SheetJS 解析后，每行只保留到"这一行最后一个非空格子"，
    // 行尾裁掉的长度各不相同——这里用 MUJI 前 6 行真实探测到的 length
    // （101/97/113/100/113/113），非空数仍是 5/8/107/92/105/108，数据行
    // 长度也是真实值 113。跟等宽版本断言同一个结果（第 6 行）。
    function makeJaggedRow(nonEmptyCount: number, rawLength: number): string[] {
      const row = Array.from({ length: rawLength }, () => '')
      for (let i = 0; i < nonEmptyCount - 1; i++) row[i] = 'v'
      row[rawLength - 1] = 'v' // 最后一格非空，行才会保留到 rawLength 这个长度
      return row
    }

    const rows = [
      makeJaggedRow(5, 101),
      makeJaggedRow(8, 97),
      makeJaggedRow(107, 113),
      makeJaggedRow(92, 100),
      makeJaggedRow(105, 113),
      makeJaggedRow(108, 113),
      makeJaggedRow(72, 113),
      makeJaggedRow(72, 113),
      makeJaggedRow(72, 113),
      makeJaggedRow(72, 113),
      makeJaggedRow(72, 113),
      makeJaggedRow(72, 113),
      makeJaggedRow(72, 113),
      makeJaggedRow(72, 113),
    ]

    expect(detectHeaderRow(rows)).toBe(6)
  })

  it('表头没被截断、数据行尾被截短：按格子数比较，不被行长带偏', () => {
    // 刻意构造的最小反例，不照抄真实文件。表头 8 个非空格子、最后一格也非
    // 空，长度停在 10；数据 5 个非空格子都挤在前面，尾部被裁掉，长度只剩
    // 5。拿各行自己的长度当分母，数据会被算成 5/5 = 1.0、比表头的 8/10 更
    // "满"，提拔判断就翻了；按格子数比较是 8 ≥ 5，表头被正确提拔。
    const header = ['h0', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', '', '', 'h9']
    const data = ['1', '2', '3', '4', '5']
    const rows = [header, data, data, data, data, data, data]

    expect(detectHeaderRow(rows)).toBe(1)
  })

  it('MUJI 形状的某个数据行在远端（第 200 列）有个孤立单元格，仍认第 6 行', () => {
    // 宽表数据行远端塞一个备注单元格在真实 Excel 里很常见。这一行的 length
    // 变成 200：只要拿"行宽"当分母（不管是全表最大行宽还是扫描区最大行
    // 宽），所有行的密度都会被压到原来的 113/200，表头堆内部的落差也跟着
    // 缩到阈值以内，判定就会落到表头堆上层。按格子数比较时，这一行只是从
    // 72 格变成 73 格。
    const rows = withStrayCell(mujiRows(), 8, 200)

    expect(detectHeaderRow(rows)).toBe(6)
  })

  it('扫描区之外的行里有离群单元格，不影响扫描区内的判定', () => {
    // 第 31 行（远在扫描区和确认窗口之外）的第 5000 列有个孤立单元格。判
    // 定只读前 DETECT_SCAN_ROWS + RUN_WINDOW_ROWS - 1 行，这一行根本不该
    // 被读到。
    const rows = mujiRows()
    while (rows.length < 31) rows.push(makeRow(72, MUJI_WIDTH))

    expect(detectHeaderRow(withStrayCell(rows, 30, 5000))).toBe(6)
  })

  it('行数很多（13 万行）时不抛异常，仍认第 6 行', () => {
    // 判定只读前面有限几行，不对全表做展开或聚合——对 13 万个元素用
    // Math.max(...array) 会抛 RangeError（参数个数超过调用栈上限）。数据
    // 行共用同一个数组对象，只是为了让用例本身不占几百 MB 内存。
    const dataRow = makeRow(72, MUJI_WIDTH)
    const rows = mujiRows()
    for (let i = 0; i < 130_000; i++) rows.push(dataRow)

    expect(detectHeaderRow(rows)).toBe(6)
  })

  it('开头有一段全空行，全空行不能当数据块起点', () => {
    // 连续 8 行全空：从第 1、2 行起的 7 行窗口全是 0 格，落差为 0，光看落
    // 差会被当成"稳定的数据块"而返回第 1 行。全空行不是数据，跳过它们，
    // 数据块从第 9 行（表头与数据格子数一致）开始。
    const blank = ['', '', '']
    const rows = [
      ...Array.from({ length: 8 }, () => blank),
      ['sku', 'color', 'size'],
      ...Array.from({ length: 8 }, () => ['4934761229522', 'Natural', 'S']),
    ]

    expect(detectHeaderRow(rows)).toBe(9)
  })

  it('表头在扫描区最后一行（第 20 行）：确认窗口要伸出扫描区去看数据', () => {
    // 前 19 行是隔行空一行的说明文字（5 格 / 0 格交替），测不出稳定；表头
    // 在第 20 行，它的数据全在第 21 行以后。起点只在扫描区里找，但窗口必
    // 须能往后读到扫描区外——否则第 20 行的窗口只剩它自己 1 行，不采信，
    // 整张表会回退到第 1 行。
    const rows: string[][] = []
    for (let i = 0; i < 19; i++) rows.push(makeRow(i % 2 === 0 ? 5 : 0, 10))
    for (let i = 0; i < 11; i++) rows.push(makeRow(10, 10))

    expect(detectHeaderRow(rows)).toBe(20)
  })

  it('行数组里有稀疏空洞（SheetJS 不传 defval 时的真实形状）不抛异常', () => {
    // sheet_to_json(sheet, { header: 1 }) 不传 defval 时，行中间的空格子是
    // 数组空洞，上游 .map(cellToString) 会原样保留空洞，按下标读出来是
    // undefined。这里在 MUJI 每一行开头挖一个洞：非空数各少 1 格，不改变
    // 判定。
    const rows = mujiRows().map((row) => {
      const copy = row.slice()
      delete copy[0]
      return copy
    })

    expect(detectHeaderRow(rows)).toBe(6)
  })
})
