import { describe, expect, it } from 'vitest'
import { detectSingleValued } from '../guidedOntology/columnStats'
import {
  addColumnToKey,
  dropField,
  findMappingConflicts,
  singleKeyColumns,
} from './mappingConflicts'
import type { BuilderEntity } from './types'

const COLUMNS = ['Order ID', 'Customer Name', 'Customer Zip Code']
/** 两个「张三」，邮编不同——事故的最小形状。 */
const ROWS = [
  ['O1', '张三', '100000'],
  ['O2', '张三', '200000'],
  ['O3', '李四', '300000'],
]

function customerEntity(overrides: Partial<BuilderEntity> = {}): BuilderEntity {
  return {
    id: 'e-customer',
    termType: 'Customer Name',
    fileId: 'f1',
    standardNameColumn: 'Customer Name',
    nodeKeyParts: [{ kind: 'column', column: 'Customer Name' }],
    fieldMappings: { Customer_Zip_Code: 'Customer Zip Code' },
    ...overrides,
  }
}

function reportFor(entities: BuilderEntity[]) {
  return detectSingleValued({
    columns: COLUMNS,
    rows: ROWS,
    hostColumns: singleKeyColumns(entities),
    attributeColumns: COLUMNS,
  })
}

describe('findMappingConflicts', () => {
  it('邮编挂在客户名下、同名客户邮编不同 → 报出来，带一个能去核对的例子', () => {
    const entities = [customerEntity()]

    const [conflict] = findMappingConflicts(entities, reportFor(entities))

    expect(conflict.termType).toBe('Customer Name')
    expect(conflict.hostColumn).toBe('Customer Name')
    expect(conflict.column).toBe('Customer Zip Code')
    expect(conflict.violation.hostValue).toBe('张三')
    expect(conflict.violation.distinctCount).toBe(2)
    expect(conflict.violation.samples).toEqual(['100000', '200000'])
  })

  it('挂在订单号下就没有冲突——订单号每行一个', () => {
    const entities = [
      customerEntity({
        id: 'e-order',
        termType: 'Order ID',
        standardNameColumn: 'Order ID',
        nodeKeyParts: [{ kind: 'column', column: 'Order ID' }],
      }),
    ]

    expect(findMappingConflicts(entities, reportFor(entities))).toEqual([])
  })

  it('组合键不给结论，而不是给一个错的', () => {
    // 组合键的每一段单独看都可能不唯一——那正是用组合键的理由。按某一段去
    // 判会把一份合法的映射误报成冲突。
    //
    // 扫描结果里**确实有** (Customer Name, Customer Zip Code) 这一对的冲突
    // （另一个实体也拿 Customer Name 当单列键时就会扫到它）。这条用例要钉的
    // 正是"明明查得到，也不能拿它去判一个组合键"。
    const composite = customerEntity({
      nodeKeyParts: [
        { kind: 'column', column: 'Customer Name' },
        { kind: 'column', column: 'Customer Zip Code' },
      ],
    })
    const report = detectSingleValued({
      columns: COLUMNS,
      rows: ROWS,
      hostColumns: ['Customer Name'],
      attributeColumns: COLUMNS,
    })
    expect(report.violationOf('Customer Name', 'Customer Zip Code')).not.toBeNull()

    expect(findMappingConflicts([composite], report)).toEqual([])
  })

  it('对数超过上限、整个检测没做时，不去读它残留的结论', () => {
    // skipped 的意思是"没检测"。这时 violationOf 的返回值没有意义——超过
    // MAX_PAIRS 时 createPairsAccumulator 一对都没建，任何"结论"都只是残留。
    // 用一个会返回冲突的 violationOf 来钉住"skipped 时根本不查"。
    const entities = [customerEntity()]
    const skipped = {
      skipped: true,
      violationOf: () => ({ hostValue: '张三', rowCount: 2, distinctCount: 2, samples: ['1', '2'] }),
    }

    expect(findMappingConflicts(entities, skipped)).toEqual([])
    expect(findMappingConflicts(entities, null)).toEqual([])
  })

  it('属性取的就是身份键那一列时不算冲突', () => {
    const entities = [
      customerEntity({ fieldMappings: { Customer_Name: 'Customer Name' } }),
    ]

    expect(findMappingConflicts(entities, reportFor(entities))).toEqual([])
  })
})

describe('两种修法', () => {
  it('把这一列也算进身份键，冲突就消失了', () => {
    const fixed = addColumnToKey(customerEntity(), 'Customer Zip Code')

    expect(fixed.nodeKeyParts).toEqual([
      { kind: 'column', column: 'Customer Name' },
      { kind: 'column', column: 'Customer Zip Code' },
    ])
    expect(findMappingConflicts([fixed], reportFor([fixed]))).toEqual([])
  })

  it('已经在键里的列不会被加第二遍', () => {
    const once = addColumnToKey(customerEntity(), 'Customer Zip Code')

    expect(addColumnToKey(once, 'Customer Zip Code')).toBe(once)
  })

  it('不导入这一列，冲突也消失，而且不动原来那个对象', () => {
    const entity = customerEntity()
    const fixed = dropField(entity, 'Customer_Zip_Code')

    expect(fixed.fieldMappings).toEqual({})
    expect(entity.fieldMappings).toEqual({ Customer_Zip_Code: 'Customer Zip Code' })
    expect(findMappingConflicts([fixed], reportFor([fixed]))).toEqual([])
  })
})

describe('singleKeyColumns', () => {
  it('只取单列键，组合键和空键不参与扫描', () => {
    const entities = [
      customerEntity(),
      customerEntity({
        id: 'e2',
        nodeKeyParts: [
          { kind: 'column', column: 'a' },
          { kind: 'column', column: 'b' },
        ],
      }),
      customerEntity({ id: 'e3', nodeKeyParts: [{ kind: 'column', column: '' }] }),
      customerEntity({
        id: 'e4',
        nodeKeyParts: [{ kind: 'allocated_code', scopeColumns: [], rawValueColumn: 'x' }],
      }),
      // 同一列被两个实体当键时只扫一次。
      customerEntity({ id: 'e5' }),
    ]

    expect(singleKeyColumns(entities)).toEqual(['Customer Name'])
  })
})
