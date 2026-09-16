import { describe, expect, it } from 'vitest'
import { alignTable, mergeAlignments, proposeCrossTableRelations } from './alignToSkeleton'
import type { ScannedTable } from './alignToSkeleton'
import type { WorkspaceState, WorkspaceTermType } from './types'
import type { ColumnRole, RoledColumn } from '../guidedOntology/types'

function column(name: string, role: ColumnRole): RoledColumn {
  return {
    stats: {
      name,
      nonEmptyCount: 100,
      distinctCount: role === 'identifier' ? 100 : 5,
      distinctCapped: false,
      samples: [],
      inferredType: 'string',
      isWholeNumber: false,
    },
    role,
    reason: '测试构造',
  }
}

function term(value: string, keyAliases: string[], fieldAliases: Record<string, string[]> = {}): WorkspaceTermType {
  return {
    value,
    display_name: value,
    provenance: 'skill',
    review: 'pending',
    standard_name_value_type: 'string',
    extra_fields: Object.keys(fieldAliases).map((name) => ({
      name,
      value_type: 'string',
      label: name,
    })),
    key_aliases: keyAliases,
    field_aliases: fieldAliases,
    clues: [],
    data_match: null,
  }
}

const SKU = term('SKU', ['jan', 'sku_code'], { color: ['现地语色', 'color'] })
const STORE = term('Store', ['store_cd'])

const emptyState = (termTypes: WorkspaceTermType[]): WorkspaceState => ({
  term_types: termTypes,
  relation_types: [],
  constraints: [],
  sources: [],
  unmatched_columns: {},
  questions: [],
})

describe('alignTable', () => {
  it('别名归一化后精确命中列名，记下是哪个别名对上的', () => {
    const table: ScannedTable = {
      file: 'sku.xls',
      roled: [column('JAN', 'identifier'), column('现地语色', 'dimension')],
    }
    const alignment = alignTable(table, [SKU, STORE])
    expect(alignment.matches).toHaveLength(1)
    expect(alignment.matches[0].termValue).toBe('SKU')
    expect(alignment.matches[0].keyColumns).toEqual(['JAN'])
    // 依据要能显示出来：用户看到"按别名 jan 对上"才判断得了对不对
    expect(alignment.matches[0].matchedBy).toBe('alias:jan')
    expect(alignment.matches[0].fieldColumns).toEqual({ color: '现地语色' })
    expect(alignment.unmatchedColumns).toEqual([])
  })

  it('命不中就是命不中，不做前缀或包含匹配', () => {
    const table: ScannedTable = { file: 'x.csv', roled: [column('jan_code_v2', 'identifier')] }
    expect(alignTable(table, [SKU]).matches).toEqual([])
    expect(alignTable(table, [SKU]).unmatchedColumns).toEqual(['jan_code_v2'])
  })

  it('已拒绝的实体类型不参与对齐', () => {
    const rejected = { ...SKU, review: 'rejected' as const }
    const table: ScannedTable = { file: 'sku.xls', roled: [column('JAN', 'identifier')] }
    expect(alignTable(table, [rejected]).matches).toEqual([])
  })

  it('没接住的列只收 identifier 和 dimension', () => {
    const table: ScannedTable = {
      file: 'x.csv',
      roled: [
        column('md_no', 'identifier'),
        column('brand', 'dimension'),
        column('revenue', 'measure'),
        column('备注', 'freetext'),
        column('创建日期', 'date'),
      ],
    }
    // 度量/自由文本/日期提升成实体类型没有意义：会给每个金额建一个节点
    expect(alignTable(table, [SKU]).unmatchedColumns).toEqual(['md_no', 'brand'])
  })

  it('已被某个实体当属性用掉的列不算没接住', () => {
    const table: ScannedTable = {
      file: 'sku.xls',
      roled: [column('JAN', 'identifier'), column('现地语色', 'dimension')],
    }
    expect(alignTable(table, [SKU]).unmatchedColumns).toEqual([])
  })

  it('A 的字段别名撞上 B 的键列时，那一列归 B 的键列，不被 A 认领做字段', () => {
    // SKU 的字段别名 warehouse 恰好写成了 store_cd——跟 Store 的键列别名
    // 同名。字段匹配要排除全部实体的键列（不只是 SKU 自己的键列 JAN），
    // 不然 SKU 会抢先把这一列认领成自己的字段，Store 的键列反而对不上。
    const skuWithClashingFieldAlias = term('SKU', ['jan'], { warehouse: ['store_cd'] })
    const table: ScannedTable = {
      file: 'x.csv',
      roled: [column('JAN', 'identifier'), column('store_cd', 'identifier')],
    }
    const alignment = alignTable(table, [skuWithClashingFieldAlias, STORE])
    const sku = alignment.matches.find((m) => m.termValue === 'SKU')
    const store = alignment.matches.find((m) => m.termValue === 'Store')
    expect(store?.keyColumns).toEqual(['store_cd'])
    expect(sku?.fieldColumns).toEqual({})
  })
})

describe('proposeCrossTableRelations', () => {
  const skuTable: ScannedTable = { file: 'sku.xls', roled: [column('JAN', 'identifier')] }
  const salesTable: ScannedTable = {
    file: 'sales.csv',
    roled: [column('JAN', 'dimension'), column('STORE_CD', 'identifier')],
  }

  it('同一实体在一张表是标识、在另一张是维度时提一条关系', () => {
    const tables = [skuTable, salesTable]
    const alignments = tables.map((t) => alignTable(t, [SKU, STORE]))
    const relations = proposeCrossTableRelations(alignments, tables, emptyState([SKU, STORE]))
    expect(relations).toHaveLength(1)
    // 主语是 dimension 那张表所属的实体（sales 表的标识是 Store），宾语是
    // identifier 那张表的实体（SKU）
    expect(relations[0].relation_type).toBe('RELATES_TO')
    expect(relations[0].provenance).toBe('data')
    expect(relations[0].review).toBe('pending')
  })

  it('骨架里已有匹配主宾的约束时，用那条约束的关系名', () => {
    const state = emptyState([SKU, STORE])
    state.constraints = [
      { subject: 'Store', relation: 'SOLD_AT', object: 'SKU', provenance: 'skill', review: 'pending' },
    ]
    const tables = [skuTable, salesTable]
    const alignments = tables.map((t) => alignTable(t, [SKU, STORE]))
    expect(proposeCrossTableRelations(alignments, tables, state)[0].relation_type).toBe('SOLD_AT')
  })

  it('两张表里角色相同时不提关系（没有方向依据）', () => {
    const a: ScannedTable = { file: 'a.csv', roled: [column('JAN', 'identifier')] }
    const b: ScannedTable = { file: 'b.csv', roled: [column('JAN', 'identifier')] }
    const tables = [a, b]
    const alignments = tables.map((t) => alignTable(t, [SKU]))
    expect(proposeCrossTableRelations(alignments, tables, emptyState([SKU]))).toEqual([])
  })
})

describe('mergeAlignments', () => {
  const table: ScannedTable = {
    file: 'sku.xls',
    roled: [column('JAN', 'identifier'), column('现地语色', 'dimension'), column('md_no', 'identifier')],
  }

  it('写入 data_match 与未接住列，不动用户的审阅决定和改名', () => {
    const state = emptyState([{ ...SKU, review: 'accepted', display_name: '商品（改过名）' }])
    const merged = mergeAlignments(state, [alignTable(table, state.term_types)], [table])
    const sku = merged.term_types[0]
    expect(sku.review).toBe('accepted')
    expect(sku.display_name).toBe('商品（改过名）')
    expect(sku.data_match).toEqual({
      source_file: 'sku.xls',
      key_columns: ['JAN'],
      field_columns: { color: '现地语色' },
      matched_by: 'alias:jan',
    })
    expect(merged.unmatched_columns).toEqual({ 'sku.xls': ['md_no'] })
  })

  it('不修改传进来的 state（页面靠引用变化判断要不要重渲染）', () => {
    const state = emptyState([SKU])
    const before = JSON.stringify(state)
    mergeAlignments(state, [alignTable(table, state.term_types)], [table])
    expect(JSON.stringify(state)).toBe(before)
  })

  it('同一张表重新扫描时覆盖上一次的结果，不追加', () => {
    const state = emptyState([SKU])
    const once = mergeAlignments(state, [alignTable(table, state.term_types)], [table])
    const twice = mergeAlignments(once, [alignTable(table, once.term_types)], [table])
    expect(twice.unmatched_columns['sku.xls']).toEqual(['md_no'])
    expect(twice.term_types[0].data_match?.key_columns).toEqual(['JAN'])
  })

  it('提议的关系进 state，重复提议不会叠加', () => {
    const skuTable: ScannedTable = { file: 'sku.xls', roled: [column('JAN', 'identifier')] }
    const salesTable: ScannedTable = {
      file: 'sales.csv',
      roled: [column('JAN', 'dimension'), column('STORE_CD', 'identifier')],
    }
    const tables = [skuTable, salesTable]
    const state = emptyState([SKU, STORE])
    const alignments = tables.map((t) => alignTable(t, state.term_types))
    const once = mergeAlignments(state, alignments, tables)
    expect(once.relation_types).toHaveLength(1)
    const twice = mergeAlignments(once, alignments, tables)
    expect(twice.relation_types).toHaveLength(1)
    expect(twice.constraints).toHaveLength(1)
  })
})
