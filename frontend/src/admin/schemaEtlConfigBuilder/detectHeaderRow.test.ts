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
    //   第 6 行：108 —— 系统代码列名，真表头（比数据更满）
    //   第 7 行起：72（各行一致）—— 数据
    // 数据块的起点会先落在第 7 行（数据本身，连续 7 行密度都是 72/113）；
    // 因为第 6 行本身非空、且密度（108/113）不低于数据块（72/113），会被
    // 往前挪一行，落到真表头第 6 行。
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
      makeRow(72, WIDTH),
      makeRow(72, WIDTH),
    ]

    expect(detectHeaderRow(rows)).toBe(6)
  })

  it('顶上的说明块比真表头还满，但它下面没有数据', () => {
    // 第 1 行本身非空格子最多，但从它开始的连续窗口一路要跨过 4 行全空行
    // 才碰到表头，密度落差很大，测不出"稳定"；表头（第 6 行）自己的密度
    // 跟后面的数据完全一致（jan/color/size 各占 3/5），数据块直接从表头
    // 这一行开始，不需要"往前挪"这一步。
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
    // 反例：表头有空列名（第三列没填），数据行反而每列都填了。数据块的
    // 起点直接落在表头这一行——表头（2/3）跟数据（3/3）的落差刚好等于
    // "3 列表容忍 1 个格子"的宽容度，两者本来就在同一个稳定窗口里，不需
    // 要"往前挪"，也不会被数据行抢先当成表头。
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
    // 表头和数据密度完全一致，数据块的起点直接落在第 1 行，扫到即返回，
    // 不会因为第 2 行往下看也一样稳定就继续找"更晚"的一行。
    const rows = [
      ['a', 'b'],
      ['1', '2'],
      ['3', '4'],
    ]

    expect(detectHeaderRow(rows)).toBe(1)
  })

  it('表头正上方还有一行说明/标题，不能认说明行', () => {
    // 用 5 列而不是 2 列构造：2 列表下，"说明行只填了半行"（密度 0.5）跟
    // "数据行缺了一格"（同样密度 0.5，见下面的窄表用例）在数字上完全等
    // 价，分不出来——这是本函数的已知局限，写在 detectHeaderRow 的文档
    // 注释里了。5 列时说明行密度（1/5）远低于表头/数据密度（5/5），落差
    // 明显超出容忍度，能被正确排除。
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
    // 5 行垃圾行密度都是 5/10，表头和数据密度都是 10/10——如果确认窗口
    // 跟垃圾行一样长（5 行），垃圾行自己就会先被误判成"稳定的数据块"。
    // RUN_WINDOW_ROWS 取 7（比垃圾行多 2 行）就是为了盖过这种情况：从垃
    // 圾行内部任何一行开始的 7 行窗口，都会探到垃圾行外面密度不同的表头/
    // 数据，跳出稳定范围。
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
    // 表头满格（10/10），数据行因为可选字段时有时无，在 8~10/10 之间波动
    // ——这种波动本身没有超出宽表的稳定阈值（0.2），表头和数据从第 1 行
    // 起就已经在同一个稳定窗口里，不需要"往前挪"。
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
    // 2 列表，缺 1 格就是 50% 的密度落差，固定阈值（0.2）对窄表天然不成
    // 立——这正是 TOLERANCE_CELLS/width 这项存在的原因：2 列表的宽容度是
    // 1/2=0.5，刚好能容下"缺 1 格"这种规模的波动。
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
    // 长度也是真实值 113。跟 makeRow 那条等宽版本断言同一个结果（第 6
    // 行），证明分母统一之后，参差的行长度不会改变判定。
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

  it('表头没有截断、数据尾部截断时，分母不统一会把表头判定反超', () => {
    // 这条不是照抄真实文件——真实 MUJI 前几行的截断幅度不足以让判定翻车
    // （被截断的行本来就会被排除），这里是刻意构造的最小反例，用来证明
    // "统一分母"这个改动是必要的，不是可有可无的稳妥做法。
    // 表头：8 个非空格子，最后一格也非空，长度停在 10（没被截断，密度 0.8）。
    // 数据：5 个非空格子都挤在前面，尾部全空被裁掉，长度只剩 5（密度 0.5，
    // 但如果拿它自己的长度当分母，会被算成 5/5=1.0，反而比表头"更满"）。
    const header = ['h0', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', '', '', 'h9']
    const data = ['1', '2', '3', '4', '5']
    const rows = [header, data, data, data, data, data, data]

    expect(detectHeaderRow(rows)).toBe(1)
  })
})
