import { beforeEach, describe, expect, it, vi } from 'vitest'
import { scanPairs, scanTableFile } from './columnStats'
import { readSourceRows } from '../schemaEtlConfigBuilder/sourceParser'

// 打桩 readSourceRows：这里要断言的是"选项被原样透传下去"，不是解析本身。
vi.mock('../schemaEtlConfigBuilder/sourceParser', () => ({
  readSourceRows: vi.fn(async () => {}),
  MAX_XLSX_BYTES: 20 * 1024 * 1024,
  TEXT_CHUNK_BYTES: 1024 * 1024,
}))

const file = new File(['a,b\n1,2\n'], 'a.csv', { type: 'text/csv' })

function optionsPassedToReader(): unknown {
  return vi.mocked(readSourceRows).mock.calls[0][1]
}

beforeEach(() => {
  vi.mocked(readSourceRows).mockClear()
})

describe('解析选项透传', () => {
  it('scanTableFile 把 options 原样交给 readSourceRows', async () => {
    await scanTableFile(file, { sheet: 'Sheet2', headerRow: 3, firstDataRow: 6 })

    expect(optionsPassedToReader()).toEqual({ sheet: 'Sheet2', headerRow: 3, firstDataRow: 6 })
  })

  it('scanPairs 把 options 原样交给 readSourceRows', async () => {
    // 两遍扫描必须用同一份解析选项，否则第二遍读的是另一张表，配对结论挂不到
    // 第一遍给出的列上，预检等于没做。后端在 Task 2 踩过这个坑。
    await scanPairs(
      file,
      { hostColumns: ['a'], attributeColumns: ['b'] },
      { sheet: 'Sheet2', headerRow: 3, firstDataRow: 6 },
    )

    expect(optionsPassedToReader()).toEqual({ sheet: 'Sheet2', headerRow: 3, firstDataRow: 6 })
  })

  it('两遍用同一份选项时，交给读取器的也是同一份', async () => {
    const options = { sheet: 1, headerRow: 2 }

    await scanTableFile(file, options)
    await scanPairs(file, { hostColumns: ['a'], attributeColumns: ['b'] }, options)

    const [first, second] = vi.mocked(readSourceRows).mock.calls
    expect(first[1]).toEqual(second[1])
    expect(second[1]).toEqual(options)
  })

  it('不传 options 时交出去的是空选项——存量调用方的行为不变', async () => {
    await scanTableFile(file)

    expect(optionsPassedToReader()).toEqual({})
  })
})
