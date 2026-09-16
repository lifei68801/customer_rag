import { describe, expect, it } from 'vitest'
import { projectToDraftPayload, projectToEtlYaml } from './projectToDraft'
import type { WorkspaceState, WorkspaceTermType } from './types'

function term(overrides: Partial<WorkspaceTermType> & { value: string }): WorkspaceTermType {
  return {
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
  }
}

const SKU = term({
  value: 'SKU',
  extra_fields: [{ name: 'color', value_type: 'string', label: '颜色' }],
  data_match: {
    source_file: 'sku.xls',
    key_columns: ['JAN'],
    field_columns: { color: '现地语色' },
    matched_by: 'alias:jan',
  },
})

const state = (overrides: Partial<WorkspaceState> = {}): WorkspaceState => ({
  term_types: [SKU],
  relation_types: [
    {
      relation_type: 'SOLD_AT',
      example_phrase: '某商品在某门店有售',
      description: '',
      provenance: 'skill',
      review: 'accepted',
      clues: [],
      data_match: null,
    },
  ],
  constraints: [
    { subject: 'SKU', relation: 'SOLD_AT', object: 'SKU', provenance: 'skill', review: 'accepted' },
  ],
  sources: [{ file: 'sku.xls', header_row: 6, first_data_row: 7 }],
  unmatched_columns: {},
  questions: [],
  ...overrides,
})

describe('projectToDraftPayload', () => {
  it('只投影 accepted 的元素', () => {
    const payload = projectToDraftPayload(
      state({
        term_types: [SKU, term({ value: '拒过的', review: 'rejected' }), term({ value: '没审的', review: 'pending' })],
      }),
    )
    expect(payload.term_types.map((t) => t.value)).toEqual(['SKU'])
  })

  it('extra_fields 原样带过去（键名就是本体表的键名）', () => {
    expect(projectToDraftPayload(state()).term_types[0].extra_fields).toEqual([
      { name: 'color', value_type: 'string', label: '颜色' },
    ])
  })

  it('约束引用的类型没被 accepted 时整条丢掉', () => {
    // 留着的话 replace_draft 会抛 UnknownCategoryError，整次应用失败，而用户
    // 看到的只是一条"引用了未声明的实体类型"——他并不知道是哪次拒绝造成的
    const payload = projectToDraftPayload(
      state({
        constraints: [
          { subject: 'SKU', relation: 'SOLD_AT', object: '没审的', provenance: 'skill', review: 'accepted' },
        ],
      }),
    )
    expect(payload.constraints).toEqual([])
  })

  it('关系类型没被 accepted 时，引用它的约束也丢掉', () => {
    const payload = projectToDraftPayload(
      state({
        relation_types: [
          {
            relation_type: 'SOLD_AT',
            example_phrase: '',
            description: '',
            provenance: 'skill',
            review: 'pending',
            clues: [],
            data_match: null,
          },
        ],
      }),
    )
    expect(payload.relation_types).toEqual([])
    expect(payload.constraints).toEqual([])
  })
})

describe('projectToEtlYaml', () => {
  it('有 data_match 的实体进 entities，键列进 node_key_parts', () => {
    const built = projectToEtlYaml(state(), 't1')
    expect(built).not.toBeNull()
    expect(built!.yaml).toContain('term_type: "SKU"')
    expect(built!.yaml).toContain('source_file: "sku.xls"')
    expect(built!.yaml).toContain('- column: "JAN"')
    expect(built!.yaml).toContain('"color": "现地语色"')
    expect(built!.fileName).toBe('sku.xls')
  })

  it('解析选项写进 sources，否则 ETL 会用第 1 行当表头', () => {
    // MUJI 那张表表头在第 6 行。漏了 sources，后端读出来的列名全是空的，
    // 而且一条错误都不报——这正是 2026-09-15 那个分支解决的问题
    const built = projectToEtlYaml(state(), 't1')
    expect(built!.yaml).toContain('sources:')
    expect(built!.yaml).toContain('header_row: 6')
    expect(built!.yaml).toContain('first_data_row: 7')
  })

  it('没有任何 data_match 时返回 null（这次应用不带映射）', () => {
    const built = projectToEtlYaml(
      state({ term_types: [term({ value: 'SKU' })] }),
      't1',
    )
    expect(built).toBeNull()
  })

  it('未 accepted 的实体不进映射', () => {
    const built = projectToEtlYaml(
      state({ term_types: [{ ...SKU, review: 'pending' }] }),
      't1',
    )
    expect(built).toBeNull()
  })
})
