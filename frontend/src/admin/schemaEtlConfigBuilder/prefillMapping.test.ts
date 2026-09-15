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


// ── 存着的映射跟当前本体对不上 ──────────────────────────────────────────
//
// 真实事故：用户把 `Product —HAS_COMPANY→ Company` 改成
// `Order ID —HAS_COMPANY→ Company` 并确认了本体，但存着的映射仍指着 Product。
// ETL 按本体校验，这些边被全部跳过，跑批报告却是"成功"——图里一条公司边都
// 没有，问"某公司有多少订单"答"没有订单"。他连撞两次都没找到原因。

const WITH_RELATION: EtlMappingSummary = {
  entities: [
    {
      term_type: 'Order ID',
      source_file: 'sales.xlsx',
      key_columns: ['Order ID'],
      key_parts: [{ kind: 'column', column: 'Order ID' }],
      name_columns: ['Order ID'],
      attributes: {},
    },
    {
      term_type: 'Product',
      source_file: 'sales.xlsx',
      key_columns: ['Product'],
      key_parts: [{ kind: 'column', column: 'Product' }],
      name_columns: ['Product'],
      attributes: {},
    },
    {
      term_type: 'Company',
      source_file: 'sales.xlsx',
      key_columns: ['Company'],
      key_parts: [{ kind: 'column', column: 'Company' }],
      name_columns: ['Company'],
      attributes: {},
    },
  ],
  relations: [
    { relation_type: 'HAS_COMPANY', subject_term_type: 'Product', object_term_type: 'Company' },
  ],
}

const COLUMNS = ['Order ID', 'Product', 'Company']

describe('存着的映射与当前本体对账', () => {
  it('本体把关系改挂到别的实体上时，映射跟着改，并说出改了什么', () => {
    const prefill = prefillMapping({
      columns: COLUMNS,
      summary: WITH_RELATION,
      termTypes: [],
      combinations: [
        { subject_term_type: 'Order ID', relation_type: 'HAS_COMPANY', object_term_type: 'Company' },
      ],
      fileId: 'f1',
    })

    expect(prefill.source).toBe('stored')
    expect(prefill.relations[0].subjectTermType).toBe('Order ID')
    expect(prefill.repairedRelations).toEqual([
      { relationType: 'HAS_COMPANY', fromSubject: 'Product', toSubject: 'Order ID' },
    ])
    expect(prefill.droppedRelations).toEqual([])
  })

  it('同名关系在本体里有多种组合时不猜，直接丢掉并报出来', () => {
    // 猜错会把边写到错误的实体上，那比不写更难发现。
    const prefill = prefillMapping({
      columns: COLUMNS,
      summary: WITH_RELATION,
      termTypes: [],
      combinations: [
        { subject_term_type: 'Order ID', relation_type: 'HAS_COMPANY', object_term_type: 'Company' },
        { subject_term_type: 'Category', relation_type: 'HAS_COMPANY', object_term_type: 'Company' },
      ],
      fileId: 'f1',
    })

    expect(prefill.relations).toEqual([])
    expect(prefill.droppedRelations).toEqual([
      { subject: 'Product', relationType: 'HAS_COMPANY', object: 'Company' },
    ])
    expect(prefill.repairedRelations).toEqual([])
  })

  it('本体里还有别的组合、但已经没有这个关系类型时，丢掉', () => {
    // 用一个非空的组合列表：空列表的含义是"本体还没拿到"，见下面那条用例。
    const prefill = prefillMapping({
      columns: COLUMNS,
      summary: WITH_RELATION,
      termTypes: [],
      combinations: [
        { subject_term_type: 'Order ID', relation_type: 'HAS_PRODUCT', object_term_type: 'Product' },
      ],
      fileId: 'f1',
    })

    expect(prefill.relations).toEqual([])
    expect(prefill.droppedRelations).toHaveLength(1)
  })

  it('关系还在本体里时原样保留，不报任何调整', () => {
    // 会误报的对账等于没有对账：用户学会忽略它之后，真出问题那次也会被忽略。
    const prefill = prefillMapping({
      columns: COLUMNS,
      summary: WITH_RELATION,
      termTypes: [],
      combinations: [
        { subject_term_type: 'Product', relation_type: 'HAS_COMPANY', object_term_type: 'Company' },
      ],
      fileId: 'f1',
    })

    expect(prefill.relations[0].subjectTermType).toBe('Product')
    expect(prefill.repairedRelations).toEqual([])
    expect(prefill.droppedRelations).toEqual([])
  })

  it('拿不到本体的允许组合时不做对账——空不等于"都不允许"', () => {
    // 页面是异步拉本体的：用户在请求回来之前选了文件，或者那个请求失败了，
    // 这里拿到的都是空列表。当成"都不允许"会把映射里的关系全部清掉，而用户
    // 什么都没做错。
    const prefill = prefillMapping({
      columns: COLUMNS,
      summary: WITH_RELATION,
      termTypes: [],
      combinations: [],
      fileId: 'f1',
    })

    expect(prefill.relations).toHaveLength(1)
    expect(prefill.relations[0].subjectTermType).toBe('Product')
    expect(prefill.droppedRelations).toEqual([])
  })
})
