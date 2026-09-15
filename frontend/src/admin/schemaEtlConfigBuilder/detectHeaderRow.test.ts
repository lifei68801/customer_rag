import { describe, expect, it } from 'vitest'
import { detectHeaderRow } from './detectHeaderRow'

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
    // 形状照真实文件的比例还原（20 列，按 113 列等比例缩小），不能简化成
    // "表头行满、别的行空"——真实文件里第 3(英文名)、4(日文名)、5(说明行)、
    // 6(系统代码，真表头) 行都相当满，表头能赢，赢在它后面连续跟着更多整
    // 行都满的数据行，而不是单看某一行的非空格数。
    const rows = [
      // 第 1 行：合并标题带，113 列里只有约 6 个非空——这里等比例缩到 1/20
      ['', '', 'Product Information', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', ''],
      // 第 2 行：全空
      ['', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', ''],
      // 第 3 行：英文列名，约 110/113 非空
      ['', 'update', 'Continue', 'RKJ Division', 'Dept', 'Class', 'Sub Class', 'Style', 'Color', 'Size', 'Season', 'Brand', 'Origin', 'Price', 'Cost', 'Weight', 'Material', 'Supplier', 'Status', 'Notes'],
      // 第 4 行：日文列名，约 100/113 非空
      ['', '', '新規継続区分', '部門', 'デパ', 'クラス', 'サブクラス', 'スタイル', 'カラー', 'サイズ', 'シーズン', 'ブランド', '原産国', '価格', '原価', '重量', '素材', 'サプライヤー', 'ステータス', ''],
      // 第 5 行：Character Limit 说明行，相当满
      ['Character Limit', 'Please enter *, if you ...', '-', '-', '-', '-', '-', '-', '-', '-', '-', '-', '-', '-', '-', '-', '-', '-', '-', ''],
      // 第 6 行：系统代码列名，113/113 全满——真表头
      ['nr', 'update_flag', 'continue_discontinue', 'sel_div', 'sel_depa', 'sel_class', 'sel_subclass', 'style_no', 'color_cd', 'size_cd', 'season_cd', 'brand_cd', 'origin_cd', 'price_amt', 'cost_amt', 'weight_kg', 'material_cd', 'supplier_cd', 'status_cd', 'notes_txt'],
      // 第 7 行起：数据，基本全满（个别可选字段留空，跟真实数据一样）
      ['1', 'Y', '5:Sales End', '1', '11', '2', 'A1', 'ST001', 'RED', 'M', 'SS24', 'BR1', 'CN', '100', '50', '0.2', 'COTTON', 'SUP1', '', ''],
      ['2', 'Y', '5:Sales End', '1', '11', '2', 'A1', 'ST002', 'BLK', 'S', 'SS24', 'BR1', 'CN', '100', '50', '0.2', 'COTTON', 'SUP1', '', ''],
      ['3', 'Y', '5:Sales End', '1', '11', '2', 'A1', 'ST003', 'NAT', 'L', 'SS24', 'BR1', 'CN', '100', '50', '0.2', 'COTTON', 'SUP1', '', ''],
      ['4', 'Y', '5:Sales End', '1', '11', '2', 'A1', 'ST004', 'RED', 'XL', 'SS24', 'BR1', 'CN', '100', '50', '0.2', 'COTTON', 'SUP1', '', ''],
      ['5', 'Y', '5:Sales End', '1', '11', '2', 'A1', 'ST005', 'BLK', 'M', 'SS24', 'BR1', 'CN', '100', '50', '0.2', 'COTTON', 'SUP1', '', ''],
      ['6', 'Y', '5:Sales End', '1', '11', '2', 'A1', 'ST006', 'NAT', 'S', 'SS24', 'BR1', 'CN', '100', '50', '0.2', 'COTTON', 'SUP1', '', ''],
    ]

    expect(detectHeaderRow(rows)).toBe(6)
  })

  it('顶上的说明块比真表头还满，但它下面没有数据', () => {
    // 这条用例是"乘上其后几行非空率"那一项存在的唯一理由。只按非空单元格
    // 数打分的话，这里会选中第 1 行——而第 1 行是一段说明文字，不是表头。
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

  it('只有一行时返回第一行', () => {
    expect(detectHeaderRow([['a', 'b']])).toBe(1)
  })

  it('空表返回第一行', () => {
    expect(detectHeaderRow([])).toBe(1)
  })

  it('并列时取最靠上的一行', () => {
    // 靠上的那一行更可能是表头：表头之后才是数据，数据行长得跟表头一样满。
    const rows = [
      ['a', 'b'],
      ['1', '2'],
      ['3', '4'],
    ]

    expect(detectHeaderRow(rows)).toBe(1)
  })
})
