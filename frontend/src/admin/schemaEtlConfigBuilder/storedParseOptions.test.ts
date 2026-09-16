import { describe, expect, it } from 'vitest'
import { storedParseOptionsFor } from './storedParseOptions'

const summary = {
  entities: [],
  relations: [],
  sources: [{ file: 'a.xls', sheet: 'Master', header_row: 6, first_data_row: 7 }],
} as never

describe('storedParseOptionsFor', () => {
  it('按文件名取出存着的选项', () => {
    expect(storedParseOptionsFor(summary, 'a.xls')).toEqual({
      sheet: 'Master',
      headerRow: 6,
      firstDataRow: 7,
    })
  })

  it('这张表没存过就返回 null，让缺省接手', () => {
    expect(storedParseOptionsFor(summary, 'b.csv')).toBeNull()
  })

  it('存量摘要没有 sources 段时返回 null，不报错', () => {
    expect(storedParseOptionsFor({ entities: [], relations: [] } as never, 'a.xls')).toBeNull()
  })

  it('null 翻译成"没设"，不翻译成字面 null', () => {
    // 把 sheet: null 原样塞进 parseOptions 的话，YAML 会写出 sheet: null，
    // 后端会拿它去找一张名叫 "null" 的工作表。
    const s = {
      entities: [],
      relations: [],
      sources: [{ file: 'a.csv', sheet: null, header_row: 1, first_data_row: null }],
    } as never

    expect(storedParseOptionsFor(s, 'a.csv')).toEqual({})
  })
})
