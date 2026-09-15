import * as XLSX from 'xlsx'
import { describe, expect, it } from 'vitest'
import { readSourceHeader, readSourcePreview, readSourceRows } from './sourceParser'

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
