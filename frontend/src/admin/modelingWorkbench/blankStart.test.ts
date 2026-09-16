import { describe, expect, it } from 'vitest'
import type { Proposal } from '../guidedOntology/types'
import { proposalToWorkspace } from './blankStart'

const proposal: Proposal = {
  termTypes: [
    {
      value: '订单号',
      extra_fields: [{ name: 'revenue', value_type: 'number', label: 'revenue' }, { name: 'field_3', value_type: 'date', label: '下单日期' }],
      standard_name_value_type: 'string',
    },
    { value: '产品', extra_fields: [], standard_name_value_type: 'string' },
  ],
  relationTypes: [{ relation_type: 'HAS_PRODUCT', example_phrase: '', description: '', allow_chain_query: true }],
  constraints: [{ subject_term_type: '订单号', relation_type: 'HAS_PRODUCT', object_term_type: '产品' }],
  unusedColumns: ['备注'],
  attributeColumns: ['revenue', '下单日期'],
  renamedFields: { 下单日期: 'field_3' },
  collidedFields: [],
  rootIsGuessed: false,
  rootName: '订单号',
  reparentedTo: { root: '订单号', names: [] },
}

describe('proposalToWorkspace', () => {
  it('实体带来源 data、待审、键别名是列名、data_match 用 column_role', () => {
    const out = proposalToWorkspace(proposal, 'orders.csv')
    const order = out.term_types.find((t) => t.value === '订单号')!
    expect(order.provenance).toBe('data')
    expect(order.review).toBe('pending')
    expect(order.key_aliases).toEqual(['订单号'])
    expect(order.data_match).toEqual({
      source_file: 'orders.csv',
      key_columns: ['订单号'],
      // 字段名 → 原列名：label 记的就是原列名
      field_columns: { revenue: 'revenue', field_3: '下单日期' },
      matched_by: 'column_role',
    })
    expect(order.field_aliases).toEqual({ revenue: ['revenue'], field_3: ['下单日期'] })
    expect(order.extra_fields).toEqual(proposal.termTypes[0].extra_fields)
  })

  it('关系与约束也是 data/pending', () => {
    const out = proposalToWorkspace(proposal, 'orders.csv')
    expect(out.relation_types[0]).toMatchObject({ relation_type: 'HAS_PRODUCT', provenance: 'data', review: 'pending', clues: [], data_match: null })
    expect(out.constraints[0]).toEqual({ subject: '订单号', relation: 'HAS_PRODUCT', object: '产品', provenance: 'data', review: 'pending' })
  })

  it('没用上的列交回去当未接住列', () => {
    expect(proposalToWorkspace(proposal, 'orders.csv').unmatched).toEqual(['备注'])
  })
})
