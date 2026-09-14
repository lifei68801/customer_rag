import { describe, expect, it } from 'vitest'
import { suggestMapping } from './suggestMapping'
import type { ConfirmedCombination, ConfirmedTermType } from './types'

const DEMO_TERM_TYPES: ConfirmedTermType[] = [
  {
    value: 'Order ID',
    extra_fields: [
      { name: 'Units_Sold', value_type: 'string', label: 'Units Sold' },
      { name: 'Revenue', value_type: 'integer', label: 'Revenue' },
    ],
  },
  {
    value: 'Customer Name',
    extra_fields: [
      { name: 'Customer_Zip_Code', value_type: 'integer', label: 'Customer Zip Code' },
    ],
  },
]

const DEMO_COMBINATIONS: ConfirmedCombination[] = [
  {
    subject_term_type: 'Order ID',
    relation_type: 'HAS_CUSTOMER_NAME',
    object_term_type: 'Customer Name',
  },
]

describe('suggestMapping', () => {
  it('实体类型对上同名列，属性按显示名找回它的源列', () => {
    const suggestion = suggestMapping({
      columns: ['Order ID', 'Units Sold', 'Revenue', 'Customer Name', 'Customer Zip Code'],
      termTypes: DEMO_TERM_TYPES,
      combinations: DEMO_COMBINATIONS,
      fileId: 'f1',
    })

    const order = suggestion.entities.find((e) => e.termType === 'Order ID')!
    expect(order.standardNameColumn).toBe('Order ID')
    expect(order.nodeKeyParts).toEqual([{ kind: 'column', column: 'Order ID' }])
    // 内部名是 Units_Sold，表头是 "Units Sold"——靠 label 才对得上。
    expect(order.fieldMappings).toEqual({ Units_Sold: 'Units Sold', Revenue: 'Revenue' })
    expect(suggestion.unusedColumns).toEqual([])
    expect(suggestion.unmatchedTermTypes).toEqual([])
  })

  it('列名大小写/分隔符跟本体不一致也能对上', () => {
    const suggestion = suggestMapping({
      columns: ['order_id', 'units sold', 'REVENUE'],
      termTypes: [DEMO_TERM_TYPES[0]],
      combinations: [],
      fileId: 'f1',
    })

    const [order] = suggestion.entities
    // 填回去的是**表头里的原样写法**，不是本体里的写法——ETL 是按列名去
    // 表里取值的，填本体的写法会取不到。
    expect(order.standardNameColumn).toBe('order_id')
    expect(order.fieldMappings).toEqual({ Units_Sold: 'units sold', Revenue: 'REVENUE' })
  })

  it('内部名跟列名完全对不上时，靠显示名找回源列', () => {
    // sanitizeFieldName 把纯中文列名清成 field_N，内部名里一个字都不剩。
    // 只按内部名找的话，这类字段永远对不上——而中文表头在这个项目里是常态。
    const suggestion = suggestMapping({
      columns: ['Order ID', '下单渠道'],
      termTypes: [
        { value: 'Order ID', extra_fields: [{ name: 'field_2', value_type: 'string', label: '下单渠道' }] },
      ],
      combinations: [],
      fileId: 'f1',
    })

    const [order] = suggestion.entities
    expect(order.fieldMappings).toEqual({ field_2: '下单渠道' })
    expect(suggestion.unusedColumns).toEqual([])
  })

  it('归一化后撞上多列时不猜，那一条留空让用户自己选', () => {
    const suggestion = suggestMapping({
      // "Revenue" 和 "revenue" 归一化后同名，选哪个都是猜。
      columns: ['Order ID', 'Revenue', 'revenue'],
      termTypes: [
        { value: 'Order ID', extra_fields: [{ name: 'Rev_enue', value_type: 'integer' }] },
      ],
      combinations: [],
      fileId: 'f1',
    })

    const [order] = suggestion.entities
    expect(order.fieldMappings).toEqual({})
  })

  it('对不上的列和对不上的实体类型都摆出来，不静默丢弃', () => {
    const suggestion = suggestMapping({
      columns: ['Order ID', 'Revenue', 'Notes'],
      termTypes: DEMO_TERM_TYPES,
      combinations: DEMO_COMBINATIONS,
      fileId: 'f1',
    })

    expect(suggestion.unusedColumns).toEqual(['Notes'])
    expect(suggestion.unmatchedTermTypes).toEqual(['Customer Name'])
    // 客体对不上，这条边没有数据来源，不该出现在建议里。
    expect(suggestion.relations).toEqual([])
  })

  it('两端都对上的组合才生成关系映射', () => {
    const suggestion = suggestMapping({
      columns: ['Order ID', 'Customer Name'],
      termTypes: DEMO_TERM_TYPES,
      combinations: DEMO_COMBINATIONS,
      fileId: 'f1',
    })

    expect(suggestion.relations).toEqual([
      {
        id: 'suggested-rel-0',
        fileId: 'f1',
        subjectTermType: 'Order ID',
        relationType: 'HAS_CUSTOMER_NAME',
        objectTermType: 'Customer Name',
      },
    ])
  })

  it('本体说邮编挂在客户名下，就照着配——对错由冲突预检回答', () => {
    // 这份建议复现的正是那次事故的映射。这个函数**不该**自作主张把邮编
    // 挪走：它只负责把本体翻译成映射，判断那份本体合不合这张表是另一件事。
    const suggestion = suggestMapping({
      columns: ['Order ID', 'Customer Name', 'Customer Zip Code'],
      termTypes: DEMO_TERM_TYPES,
      combinations: [],
      fileId: 'f1',
    })

    const customer = suggestion.entities.find((e) => e.termType === 'Customer Name')!
    expect(customer.fieldMappings).toEqual({ Customer_Zip_Code: 'Customer Zip Code' })
  })
})
