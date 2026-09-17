import { describe, expect, it } from 'vitest'
import {
  assignColumnAsField,
  assignColumnAsKey,
  parseAliasText,
  setFieldAliases,
  setKeyAliases,
} from './columnAssign'
import type { WorkspaceState, WorkspaceTermType } from './types'

const term = (overrides: Partial<WorkspaceTermType> & { value: string }): WorkspaceTermType => ({
  display_name: overrides.value,
  provenance: 'skill',
  review: 'accepted',
  standard_name_value_type: 'string',
  extra_fields: [],
  key_aliases: [],
  field_aliases: {},
  clues: [],
  data_match: null,
  ...overrides,
})

const base = (): WorkspaceState => ({
  term_types: [term({ value: 'SKU', key_aliases: ['jan'] }), term({ value: 'Store', review: 'rejected' })],
  relation_types: [],
  constraints: [],
  sources: [
    {
      file: 'sku.xls',
      columns: [
        { name: '商品コード', role: 'identifier', reason: '每行一个', inferred_type: 'string' },
        { name: '売価', role: 'measure', reason: '带小数', inferred_type: 'number' },
        { name: '色', role: 'dimension', reason: '5 个取值', inferred_type: 'string' },
      ],
    },
  ],
  unmatched_columns: { 'sku.xls': ['商品コード', '売価', '色'] },
  questions: [],
})

describe('assignColumnAsKey', () => {
  it('写 data_match、追加键别名、从未接住里移除', () => {
    const next = assignColumnAsKey(base(), 'sku.xls', '商品コード', 'SKU')
    const sku = next.term_types[0]
    expect(sku.data_match).toEqual({
      source_file: 'sku.xls',
      key_columns: ['商品コード'],
      field_columns: {},
      matched_by: 'manual',
    })
    // 手动指的别名放最前：下次重扫时它先命中，不会被旧别名 jan 抢先翻回去
    expect(sku.key_aliases).toEqual(['商品コード', 'jan'])
    expect(next.unmatched_columns['sku.xls']).toEqual(['売価', '色'])
  })

  it('同一张表已有字段匹配时保留字段，换表时清空字段', () => {
    const withFields = base()
    withFields.term_types[0].data_match = {
      source_file: 'sku.xls',
      key_columns: ['JAN'],
      field_columns: { color: '色' },
      matched_by: 'alias:jan',
    }
    expect(assignColumnAsKey(withFields, 'sku.xls', '商品コード', 'SKU').term_types[0].data_match!.field_columns).toEqual({ color: '色' })
    expect(assignColumnAsKey(withFields, 'other.csv', 'x', 'SKU').term_types[0].data_match!.field_columns).toEqual({})
  })

  it('指给已拒绝或不存在的实体原样返回', () => {
    const s = base()
    expect(assignColumnAsKey(s, 'sku.xls', '商品コード', 'Store')).toBe(s)
    expect(assignColumnAsKey(s, 'sku.xls', '商品コード', 'Nope')).toBe(s)
  })

  it('不改入参', () => {
    const s = base()
    const snapshot = JSON.stringify(s)
    assignColumnAsKey(s, 'sku.xls', '商品コード', 'SKU')
    expect(JSON.stringify(s)).toBe(snapshot)
  })
})

describe('assignColumnAsField', () => {
  const keyed = (): WorkspaceState => assignColumnAsKey(base(), 'sku.xls', '商品コード', 'SKU')

  it('加字段、写 field_columns 与字段别名、value_type 按推断类型', () => {
    const next = assignColumnAsField(keyed(), 'sku.xls', '売価', 'SKU')
    const sku = next.term_types[0]
    // 中文/日文列名清洗后是空壳，回落成 field_N；显示名记原列名
    const field = sku.extra_fields[0]
    expect(field.label).toBe('売価')
    expect(field.value_type).toBe('number')
    expect(sku.data_match!.field_columns[field.name]).toBe('売価')
    expect(sku.field_aliases[field.name]).toEqual(['売価'])
    expect(next.unmatched_columns['sku.xls']).toEqual(['色'])
  })

  it('实体还没在这张表有键列时拒绝（原样返回）', () => {
    const s = base()
    expect(assignColumnAsField(s, 'sku.xls', '売価', 'SKU')).toBe(s)
  })

  it('字段内部名与已有字段撞名时加后缀', () => {
    const s = keyed()
    s.term_types[0].extra_fields = [{ name: 'price', value_type: 'number', label: '价' }]
    s.sources[0].columns!.push({ name: 'price', role: 'measure', reason: '', inferred_type: 'number' })
    s.unmatched_columns['sku.xls'].push('price')
    const next = assignColumnAsField(s, 'sku.xls', 'price', 'SKU')
    expect(next.term_types[0].extra_fields.map((f) => f.name)).toEqual(['price', 'price_2'])
  })

  it('超长列名撞名后仍不超过后端的 64 字符上限', () => {
    // 后端字段名正则是 ^[a-zA-Z_][a-zA-Z0-9_]{0,63}$——拼后缀前不截断的话，
    // 一个 63 字符的列名撞名后会变成 65 字符，写草稿时被 400 挡下。
    const longName = 'a'.repeat(63)
    const s = keyed()
    s.term_types[0].extra_fields = [{ name: longName, value_type: 'string', label: longName }]
    s.sources[0].columns!.push({ name: longName, role: 'dimension', reason: '', inferred_type: 'string' })
    s.unmatched_columns['sku.xls'].push(longName)
    const added = assignColumnAsField(s, 'sku.xls', longName, 'SKU').term_types[0].extra_fields[1]
    expect(added.name.length).toBeLessThanOrEqual(64)
    expect(added.name).not.toBe(longName)
  })
})

describe('别名编辑', () => {
  it('parseAliasText 按中英文逗号拆、去空白、去重、去空', () => {
    expect(parseAliasText(' jan，JAN, sku_code ,, jan ')).toEqual(['jan', 'JAN', 'sku_code'])
    expect(parseAliasText('')).toEqual([])
  })

  it('setKeyAliases 整份替换', () => {
    const next = setKeyAliases(base(), 'SKU', ['sku', '品番'])
    expect(next.term_types[0].key_aliases).toEqual(['sku', '品番'])
  })

  it('setFieldAliases 只改指定字段，字段不存在时原样返回', () => {
    const s = base()
    s.term_types[0].extra_fields = [{ name: 'color', value_type: 'string', label: '颜色' }]
    s.term_types[0].field_aliases = { color: ['色'] }
    expect(setFieldAliases(s, 'SKU', 'color', ['色', 'colour']).term_types[0].field_aliases).toEqual({ color: ['色', 'colour'] })
    expect(setFieldAliases(s, 'SKU', 'size', ['x'])).toBe(s)
  })
})
