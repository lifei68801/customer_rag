import { describe, expect, it } from 'vitest'
import { prefillMapping } from './prefillMapping'
import type { EtlMappingSummary } from '../etlMappingApi'
import type { ConfirmedTermType } from './types'

const SUMMARY: EtlMappingSummary = {
  entities: [
    {
      term_type: 'Order ID',
      source_file: 'sales.xlsx',
      key_columns: ['Order ID'],
      key_parts: [{ kind: 'column', column: 'Order ID' }],
      name_columns: ['Order ID'],
      attributes: { Revenue: 'Revenue' },
    },
  ],
  relations: [],
}

const TERM_TYPES: ConfirmedTermType[] = [
  { value: 'Order ID', extra_fields: [{ name: 'Revenue', value_type: 'integer', label: 'Revenue' }] },
]

describe('prefillMapping', () => {
  it('存着的映射引用的列这张表都有 → 沿用它', () => {
    const prefill = prefillMapping({
      columns: ['Order ID', 'Revenue', 'Notes'],
      summary: SUMMARY,
      termTypes: TERM_TYPES,
      combinations: [],
      fileId: 'f1',
    })

    expect(prefill.source).toBe('stored')
    expect(prefill.entities[0].id).toBe('stored-Order ID')
    expect(prefill.unusedColumns).toEqual(['Notes'])
    expect(prefill.storedMissingColumns).toBeNull()
  })

  it('存着的映射引用了这张表没有的列 → 改推建议，并说出缺哪几列', () => {
    // 不说原因的话，用户看到的是"这不是我上次配的那份"，最可能的猜测是
    // 系统把配置弄丢了；真实原因通常是他换了一张列名不同的表。
    const prefill = prefillMapping({
      columns: ['Order ID', 'Revenue'],
      summary: {
        ...SUMMARY,
        entities: [{ ...SUMMARY.entities[0], attributes: { Revenue: '营业额' } }],
      },
      termTypes: TERM_TYPES,
      combinations: [],
      fileId: 'f1',
    })

    expect(prefill.source).toBe('suggested')
    expect(prefill.storedMissingColumns).toEqual(['营业额'])
    expect(prefill.entities[0].id).toBe('suggested-Order ID')
  })

  it('身份键里的分配规则引用的列也算数', () => {
    // 只看 name_columns 和 attributes 的话，这份映射会被当成"能沿用"，
    // 而它的身份键指着一列根本不存在的 brand——跑批才会失败。
    const prefill = prefillMapping({
      columns: ['Order ID', 'Revenue'],
      summary: {
        ...SUMMARY,
        entities: [
          {
            ...SUMMARY.entities[0],
            key_parts: [
              { kind: 'allocated_code', scope_columns: ['brand'], raw_value_column: 'Order ID' },
            ],
          },
        ],
      },
      termTypes: TERM_TYPES,
      combinations: [],
      fileId: 'f1',
    })

    expect(prefill.source).toBe('suggested')
    expect(prefill.storedMissingColumns).toEqual(['brand'])
  })

  it('没有存过映射 → 直接给建议', () => {
    const prefill = prefillMapping({
      columns: ['Order ID', 'Revenue'],
      summary: null,
      termTypes: TERM_TYPES,
      combinations: [],
      fileId: 'f1',
    })

    expect(prefill.source).toBe('suggested')
    expect(prefill.storedMissingColumns).toBeNull()
    expect(prefill.entities).toHaveLength(1)
  })
})
