import type { PairReport, PairViolation } from '../guidedOntology/columnStats'
import type { BuilderEntity } from './types'

/**
 * 「这份映射跑下去会被拒」——运行之前就能算出来的那一类。
 *
 * ## 拦的是哪件事
 *
 * ETL 对同一个 node_key 会算一份指纹（展示名 + 全部属性）。同一个 node_key
 * 出现在多行是正常的（一个客户很多订单），但**指纹必须一致**：算出两份不同
 * 的指纹，说明这个键代表不了一个稳定的实体，整批拒绝写入（DuplicateNodeKeyError）。
 *
 * 真实事故：邮编挂在「客户名」下，而 530 个客户名各自对应不止一个邮编。跑批
 * 报的是"实体类型 'Customer Name' 有 530 个 node_key 被算出了不同的值"——
 * 一句只有读过 ETL 源码的人才懂的话，而且是**跑完才说**。
 *
 * 这件事在跑之前就算得出来：扫一遍表，看每个身份键值是不是只对应一个属性值。
 * 扫描本身在 guidedOntology/columnStats.scanPairs（流式、两遍、已有测试），
 * 这里只负责把扫描结果翻译成"哪个实体的哪个属性有问题"。
 *
 * ## 为什么只看单列身份键
 *
 * 组合键的每一段单独看都可能不唯一——那正是用组合键的理由。判一个组合键下
 * 属性是否单值，要按拼起来的值分组，那是另一套扫描。单列键覆盖了绝大多数
 * 情况，也是出事的那一类；组合键这里如实地不给结论，而不是给一个错的。
 */
export interface MappingConflict {
  entityId: string
  termType: string
  /** 出问题的身份键列。 */
  hostColumn: string
  /** 属性的内部字段名。 */
  field: string
  /** 这个属性取自哪一列。 */
  column: string
  violation: PairViolation
}

/** 这份映射里，每个实体的单列身份键。组合键和分配编号不参与检测。 */
export function singleKeyColumns(entities: BuilderEntity[]): string[] {
  const columns = entities
    .filter((e) => e.nodeKeyParts.length === 1 && e.nodeKeyParts[0].kind === 'column')
    .map((e) => (e.nodeKeyParts[0] as { column: string }).column)
    .filter((c) => c !== '')
  return [...new Set(columns)]
}

export function findMappingConflicts(
  entities: BuilderEntity[],
  report: PairReport | null,
): MappingConflict[] {
  if (report === null || report.skipped) return []
  const conflicts: MappingConflict[] = []
  for (const entity of entities) {
    if (entity.nodeKeyParts.length !== 1) continue
    const part = entity.nodeKeyParts[0]
    if (part.kind !== 'column' || part.column === '') continue
    for (const [field, column] of Object.entries(entity.fieldMappings)) {
      // 属性取的就是身份键那一列时不可能冲突，scanPairs 本来也不会为它建对。
      if (column === part.column) continue
      const violation = report.violationOf(part.column, column)
      if (violation) {
        conflicts.push({
          entityId: entity.id,
          termType: entity.termType,
          hostColumn: part.column,
          field,
          column,
          violation,
        })
      }
    }
  }
  return conflicts
}

/**
 * 修法一：把这一列也算进身份键。
 *
 * 「同名不同邮编」于是变成两个不同的客户节点。这是**不改本体**就能做的
 * 修法，代价是同一个人的两个地址会被拆成两个节点——所以界面上要把这句
 * 代价说出来，而不是当成"一键修复"。
 */
export function addColumnToKey(entity: BuilderEntity, column: string): BuilderEntity {
  if (entity.nodeKeyParts.some((p) => p.kind === 'column' && p.column === column)) return entity
  return { ...entity, nodeKeyParts: [...entity.nodeKeyParts, { kind: 'column', column }] }
}

/** 修法二：这一列不导入。数据不进图，但也不会再挡住整批写入。 */
export function dropField(entity: BuilderEntity, field: string): BuilderEntity {
  const fieldMappings = { ...entity.fieldMappings }
  delete fieldMappings[field]
  return { ...entity, fieldMappings }
}
