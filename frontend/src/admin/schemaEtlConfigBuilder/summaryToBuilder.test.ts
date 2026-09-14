import { describe, expect, it } from 'vitest'
import { summaryToBuilder } from './summaryToBuilder'
import type { EtlMappingSummary } from '../etlMappingApi'

const SUMMARY: EtlMappingSummary = {
  entities: [
    {
      term_type: 'Order ID',
      source_file: 'soft_drink_sales.xlsx',
      key_columns: ['Order ID'],
      key_parts: [{ kind: 'column', column: 'Order ID' }],
      name_columns: ['Order ID'],
      attributes: { Revenue: 'Revenue' },
    },
    {
      term_type: 'SKU',
      source_file: 'soft_drink_sales.xlsx',
      key_columns: ['brand', '按「label」分配编号'],
      key_parts: [
        { kind: 'column', column: 'brand' },
        { kind: 'allocated_code', scope_columns: ['brand'], raw_value_column: 'label' },
      ],
      name_columns: ['label'],
      attributes: {},
    },
  ],
  relations: [
    { relation_type: 'HAS_SKU', subject_term_type: 'Order ID', object_term_type: 'SKU' },
  ],
}

describe('summaryToBuilder', () => {
  it('实体、身份键、属性、关系都填回编辑器的形状', () => {
    const { entities, relations } = summaryToBuilder(SUMMARY, 'f1')

    expect(entities[0]).toEqual({
      id: 'stored-Order ID',
      termType: 'Order ID',
      fileId: 'f1',
      standardNameColumn: 'Order ID',
      nodeKeyParts: [{ kind: 'column', column: 'Order ID' }],
      fieldMappings: { Revenue: 'Revenue' },
    })
    expect(relations).toEqual([
      {
        id: 'stored-rel-0',
        fileId: 'f1',
        subjectTermType: 'Order ID',
        relationType: 'HAS_SKU',
        objectTermType: 'SKU',
      },
    ])
  })

  it('分配编号那种身份键原样还原，不降级成一个叫那句中文的列', () => {
    // 拿 key_columns 填的话，这里会出现 { kind:'column', column:'按「label」分配编号' }
    // ——一个表里根本不存在的列名，而界面上看不出发生过降级。
    const { entities } = summaryToBuilder(SUMMARY, 'f1')

    expect(entities[1].nodeKeyParts).toEqual([
      { kind: 'column', column: 'brand' },
      { kind: 'allocated_code', scopeColumns: ['brand'], rawValueColumn: 'label' },
    ])
  })

  it('全部实体指向这次选的文件，不是存映射时那张表', () => {
    const { entities, relations } = summaryToBuilder(SUMMARY, 'f-new')

    expect(entities.every((e) => e.fileId === 'f-new')).toBe(true)
    expect(relations.every((r) => r.fileId === 'f-new')).toBe(true)
  })

  it('属性是拷贝，改了填回来的映射不会反过来改存着的那份', () => {
    const { entities } = summaryToBuilder(SUMMARY, 'f1')
    entities[0].fieldMappings.Revenue = '别的列'

    expect(SUMMARY.entities[0].attributes).toEqual({ Revenue: 'Revenue' })
  })
})
