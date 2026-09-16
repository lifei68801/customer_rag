import * as XLSX from 'xlsx'
import { describe, expect, it } from 'vitest'
import {
  readSourceHeader,
  readSourcePreview,
  readSourceRows,
  sameEffectiveParseOptions,
  trimTrailingEmptyNames,
} from './sourceParser'

function csvFile(name: string, text: string): File {
  return new File([text], name, { type: 'text/csv' })
}

function xlsxFile(name: string, sheets: Record<string, unknown[][]>): File {
  const workbook = XLSX.utils.book_new()
  for (const [sheetName, rows] of Object.entries(sheets)) {
    XLSX.utils.book_append_sheet(workbook, XLSX.utils.aoa_to_sheet(rows), sheetName)
  }
  const buffer = XLSX.write(workbook, { type: 'array', bookType: 'xlsx' }) as ArrayBuffer
  return new File([buffer], name, {
    type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  })
}

describe('readSourceHeader', () => {
  it('缺省读第一行——存量行为不能变', async () => {
    const file = csvFile('a.csv', 'Color,Size\nNatural,S\n')

    expect(await readSourceHeader(file)).toEqual(['Color', 'Size'])
  })

  it('按 headerRow 取表头', async () => {
    const file = csvFile('a.csv', '标题带,\nmd_no,color\nM1AG702,Natural\n')

    expect(await readSourceHeader(file, { headerRow: 2 })).toEqual(['md_no', 'color'])
  })

  it('重名列加后缀——必须跟后端一致，否则第二列在界面上无法寻址', async () => {
    const file = csvFile('a.csv', 'Color,Color\na,b\n')

    expect(await readSourceHeader(file)).toEqual(['Color', 'Color (2)'])
  })

  it('表头行号超出文件长度时返回空', async () => {
    const file = csvFile('a.csv', 'a,b\n1,2\n')

    expect(await readSourceHeader(file, { headerRow: 9 })).toEqual([])
  })
})

describe('readSourceRows', () => {
  it('跳过表头和首数据行之间的说明行', async () => {
    const file = csvFile('a.csv', 'Article Code,Color\n-,(half width 100)\nM1AG702,Natural\n')
    const rows: string[][] = []

    await readSourceRows(
      file,
      { headerRow: 1, firstDataRow: 3 },
      () => {},
      (r) => rows.push(r),
    )

    expect(rows).toEqual([['M1AG702', 'Natural']])
  })

  it('缺省下首数据行紧跟表头', async () => {
    const file = csvFile('a.csv', 'a,b\n1,2\n3,4\n')
    const rows: string[][] = []

    await readSourceRows(
      file,
      {},
      () => {},
      (r) => rows.push(r),
    )

    expect(rows).toEqual([
      ['1', '2'],
      ['3', '4'],
    ])
  })
})

describe('readSourcePreview', () => {
  it('给出原始的前几行，不受 headerRow 影响——探测和预览要看的就是原始形状', async () => {
    const file = csvFile('a.csv', '标题带,\nmd_no,color\nM1AG702,Natural\n')

    expect(await readSourcePreview(file, 2)).toEqual([
      ['标题带', ''],
      ['md_no', 'color'],
    ])
  })
})

describe('工作表选择', () => {
  it('按名字选中指定的工作表，不是第一张', async () => {
    const file = xlsxFile('book.xlsx', {
      First: [['a'], ['1']],
      Second: [['b'], ['2']],
    })

    expect(await readSourceHeader(file, { sheet: 'Second' })).toEqual(['b'])
  })

  it('指定的工作表不存在时报错，不回落到第一张——回落会让用户拿到一份完全不相干的数据', async () => {
    const file = xlsxFile('book.xlsx', {
      First: [['a'], ['1']],
      Second: [['b'], ['2']],
    })

    await expect(readSourceHeader(file, { sheet: 'Missing' })).rejects.toThrow(/Missing/)
    await expect(
      readSourceRows(
        file,
        { sheet: 'Missing' },
        () => {},
        () => {},
      ),
    ).rejects.toThrow(/Missing/)
  })
})

describe('Excel 路径的解析选项', () => {
  it('按 headerRow / firstDataRow 取表头和数据——Excel 侧跟 CSV 侧同一套行号语义', async () => {
    const file = xlsxFile('book.xlsx', {
      Sheet1: [
        ['标题带'],
        ['md_no', 'color'],
        ['-', '(half width 100)'],
        ['M1AG702', 'Natural'],
      ],
    })
    const rows: string[][] = []
    let header: string[] = []

    await readSourceRows(
      file,
      { headerRow: 2, firstDataRow: 4 },
      (columns) => {
        header = columns
      },
      (r) => rows.push(r),
    )

    expect(header).toEqual(['md_no', 'color'])
    expect(rows).toEqual([['M1AG702', 'Natural']])
    expect(await readSourceHeader(file, { headerRow: 2 })).toEqual(['md_no', 'color'])
  })

  it('Excel 的表头同样过 deduplicateHeader——两条路径不能对同一张表给出不同的列名', async () => {
    const file = xlsxFile('dup.xlsx', { Sheet1: [['Color', 'Color'], ['a', 'b']] })
    let header: string[] = []

    await readSourceRows(
      file,
      {},
      (columns) => {
        header = columns
      },
      () => {},
    )

    expect(header).toEqual(['Color', 'Color (2)'])
    expect(await readSourceHeader(file)).toEqual(['Color', 'Color (2)'])
  })
})

describe('解析选项校验', () => {
  const file = csvFile('a.csv', 'a,b\n1,2\n')

  it('headerRow 小于 1 被拒绝——行号跟 Excel 一样从 1 开始', async () => {
    await expect(readSourceHeader(file, { headerRow: 0 })).rejects.toThrow(/headerRow/)
  })

  it('firstDataRow 不在 headerRow 之后被拒绝', async () => {
    // 不拒绝的话两条路径的结果还不一样：CSV 路径会把表头之前的行挡掉，
    // Excel 路径会把表头行及其上方的行当成数据行发出去。
    await expect(
      readSourceRows(
        file,
        { headerRow: 2, firstDataRow: 1 },
        () => {},
        () => {},
      ),
    ).rejects.toThrow(/firstDataRow/)
  })

  it('sheet 序号是负数被拒绝', async () => {
    await expect(readSourcePreview(file, 1, { sheet: -1 })).rejects.toThrow(/sheet/)
  })
})

describe('Excel 行形状', () => {
  /**
   * 第 3 行中间和行尾都是空格子：不给 sheet_to_json 传 defval 时，SheetJS 会
   * 截掉行尾的空格子、并在行中间留下稀疏空洞。CSV 路径和后端（xlrd）给出的
   * 都是满宽行，前端 Excel 路径不一致的话，同一张表在两端会有不同的列数。
   */
  function raggedFile(): File {
    return xlsxFile('ragged.xlsx', {
      Sheet1: [
        ['a', 'b', 'c', 'd'],
        ['1', '2', '3', '4'],
        // 行中间有空洞，行尾有值——尾部留值是故意的：尾部空名列会被
        // trimTrailingEmptyNames 砍掉，那是另一组用例在管的事，混在一起
        // 这组就测不出"稀疏空洞"本身了。
        ['p', null, 'r', 's'],
      ],
    })
  }

  /** 满宽且无空洞：稀疏数组的 length 大于它实际拥有的下标个数。 */
  function isDense(row: unknown[], width: number): boolean {
    return row.length === width && Object.keys(row).length === width
  }

  it('表头行取到满宽、无空洞的列名', async () => {
    const header = await readSourceHeader(raggedFile(), { headerRow: 3 })

    expect(header).toHaveLength(4)
    expect(header.every((cell) => typeof cell === 'string')).toBe(true)
  })

  it('预览行是满宽、无空洞的', async () => {
    const rows = await readSourcePreview(raggedFile(), 3)

    expect(rows.every((row) => isDense(row, 4))).toBe(true)
  })

  it('数据行是满宽、无空洞的', async () => {
    const rows: string[][] = []

    await readSourceRows(
      raggedFile(),
      { headerRow: 1 },
      () => {},
      (r) => rows.push(r),
    )

    expect(rows).toHaveLength(2)
    expect(rows.every((row) => isDense(row, 4))).toBe(true)
  })
})

describe('sameEffectiveParseOptions', () => {
  it('补齐缺省之后一样就算一样——把 1 原样打一遍不算改过', () => {
    expect(sameEffectiveParseOptions({}, { headerRow: 1 })).toBe(true)
    expect(sameEffectiveParseOptions({ headerRow: 2 }, { headerRow: 2, firstDataRow: 3 })).toBe(true)
  })

  it('读的不是同一批行就算改过', () => {
    // 这三种改法都不会改变列名，因而不会被跑批前的列名对账发现——判不出
    // 它们改过的话，解析设置不会被发给后端，数据会安静地读错。
    expect(sameEffectiveParseOptions({ headerRow: 1 }, { headerRow: 1, firstDataRow: 5 })).toBe(false)
    expect(sameEffectiveParseOptions({}, { sheet: 'Work' })).toBe(false)
    expect(sameEffectiveParseOptions({ sheet: 'Master' }, { sheet: 1 })).toBe(false)
  })
})

describe('声明范围比实际单元格宽的表', () => {
  /**
   * 手工编辑过的 Excel 里，工作表声明的范围（`!ref`）常常比真正有单元格的
   * 范围宽——真实的 MUJI `CN_001_SKU_MASTER_121.xls` 就是这样：`!ref` 是
   * `A1:DJ908`（114 列），而第 114 列一个单元格都没有，后端 xlrd 的
   * `ncols` 按实际存在的单元格记录算，给出 113。
   *
   * 两边列数对不上，Task 3 的逐列对账会直接判 400，这张表根本导不进去。
   */
  function overWideRefFile(bookType: 'xlsx' | 'biff8'): File {
    const workbook = XLSX.utils.book_new()
    const sheet = XLSX.utils.aoa_to_sheet([
      ['md_no', 'color', 'size'],
      ['M1AG702', 'Natural', 'M'],
      ['M1AG703', 'Black', 'L'],
    ])
    // 比实际单元格多两列、多两行。写出去再读回来时这个声明会原样保留
    // （xlsx 和 BIFF8 都是，实测过）。
    sheet['!ref'] = 'A1:E5'
    XLSX.utils.book_append_sheet(workbook, sheet, 'Sheet1')
    const buffer = XLSX.write(workbook, { type: 'array', bookType }) as ArrayBuffer
    return new File([buffer], bookType === 'xlsx' ? 'overwide.xlsx' : 'overwide.xls')
  }

  // 两种格式都测：xlsx 的读取器在 sheetRows 限行时会顺带按实际单元格重算
  // !ref，BIFF8 不会。只测 xlsx 的话，readSourceHeader 会因为这个副作用
  // 假绿——而真实的 MUJI 表正是 .xls。
  for (const bookType of ['xlsx', 'biff8'] as const) {
    describe(bookType, () => {
      it('列名按实际有单元格的列算，不按声明范围算', async () => {
        expect(await readSourceHeader(overWideRefFile(bookType))).toEqual([
          'md_no',
          'color',
          'size',
        ])
      })

      it('预览不铺出声明范围里那几列空气，也不铺空行', async () => {
        expect(await readSourcePreview(overWideRefFile(bookType), 10)).toEqual([
          ['md_no', 'color', 'size'],
          ['M1AG702', 'Natural', 'M'],
          ['M1AG703', 'Black', 'L'],
        ])
      })

      it('数据行同样按实际单元格算', async () => {
        const rows: string[][] = []

        await readSourceRows(
          overWideRefFile(bookType),
          {},
          () => {},
          (r) => rows.push(r),
        )

        expect(rows).toEqual([
          ['M1AG702', 'Natural', 'M'],
          ['M1AG703', 'Black', 'L'],
        ])
      })

      it('第一列整列没有单元格时，它仍然占着第一列的位置', async () => {
        // 范围的起点固定在 A1，不跟着 !ref 走：xlrd 的列下标从 0 起算，A 列
        // 空着也照样占一个位置。跟着 !ref 走的话，前端会把 B 列当成第 0 列，
        // 两边的列名整体错开一位，而列数还是一样的——对账发现不了。
        const workbook = XLSX.utils.book_new()
        const sheet = XLSX.utils.aoa_to_sheet([
          [null, 'color', 'size'],
          [null, 'Natural', 'M'],
        ])
        sheet['!ref'] = 'B1:C2'
        XLSX.utils.book_append_sheet(workbook, sheet, 'Sheet1')
        const buffer = XLSX.write(workbook, { type: 'array', bookType }) as ArrayBuffer
        const file = new File([buffer], bookType === 'xlsx' ? 'offset.xlsx' : 'offset.xls')

        expect(await readSourceHeader(file)).toEqual(['', 'color', 'size'])
      })
    })
  }
})

describe('trimTrailingEmptyNames', () => {
  it('砍掉行尾连续的空名列', () => {
    expect(trimTrailingEmptyNames(['a', 'b', 'c', '', ''])).toEqual(['a', 'b', 'c'])
  })

  it('中间的空名列一列都不动', () => {
    // 砍掉中间那个，后面所有列的位置会整体左移——而列数还是对得上的，
    // 跑批前的对账发现不了。
    expect(trimTrailingEmptyNames(['a', '', 'c'])).toEqual(['a', '', 'c'])
  })

  it('全是空名时砍成空数组，不报错', () => {
    expect(trimTrailingEmptyNames(['', '  '])).toEqual([])
  })
})

describe('列数口径跟后端对齐', () => {
  it('CSV 行尾多出来的逗号不算列', async () => {
    const file = csvFile('trailing.csv', 'a,b,c,,\n1,2,3,,\n')

    expect(await readSourceHeader(file)).toEqual(['a', 'b', 'c'])
  })

  it('Excel 行尾的空名列不算列', async () => {
    const file = xlsxFile('trailing.xlsx', {
      Sheet1: [
        ['a', 'b', 'c', '', ''],
        ['1', '2', '3', '', ''],
      ],
    })

    expect(await readSourceHeader(file)).toEqual(['a', 'b', 'c'])
  })

  it('中间的空名列照常参与去重，位置不变', async () => {
    const file = xlsxFile('middle.xlsx', {
      Sheet1: [
        ['a', '', 'c'],
        ['1', '2', '3'],
      ],
    })

    expect(await readSourceHeader(file)).toEqual(['a', '', 'c'])
  })
})
