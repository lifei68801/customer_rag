import type { EtlMappingSummary } from '../etlMappingApi'
import type { BuilderEntity, BuilderRelation, NodeKeyPart } from './types'

/**
 * 把存好的那份映射填回可编辑的表单。
 *
 * 表格导入页的第二步要"已经填好、但能改"。已有映射时那份预填就该来自
 * **存着的那一份**，而不是重新推一份建议——重推出来的东西跟用户上次确认
 * 过的可能不一样，而界面上写着"沿用上次"。
 *
 * 读 summary 而不是 config_yaml：前端不解析 YAML（没装解析器，装一个只为
 * 读回自己写出去的东西也不合算），后端已经把这份配置摊成了 summary。
 *
 * fileId 由调用方给：summary 里的 source_file 是**存映射时**那张表的文件名，
 * 而这次跑批用的是用户刚选的文件，两者不必同名——后端本来就允许"只传一个
 * 文件时名字跟映射里的不一样"。所有实体一律指向这次选的文件。
 */
export function summaryToBuilder(
  summary: EtlMappingSummary,
  fileId: string,
): { entities: BuilderEntity[]; relations: BuilderRelation[] } {
  const entities: BuilderEntity[] = summary.entities.map((entity) => ({
    id: `stored-${entity.term_type}`,
    termType: entity.term_type,
    fileId,
    // 存的是多列拼接时，编辑器只放得下一列。取第一列并不丢数据——用户不动
    // 它的话，这份映射会按存着的原样重新提交（见 SchemaEtlPage 的提交路径）。
    standardNameColumn: entity.name_columns[0] ?? '',
    nodeKeyParts: entity.key_parts.map(toNodeKeyPart),
    fieldMappings: { ...entity.attributes },
  }))

  const relations: BuilderRelation[] = summary.relations.map((relation, index) => ({
    id: `stored-rel-${index}`,
    fileId,
    subjectTermType: relation.subject_term_type,
    relationType: relation.relation_type,
    objectTermType: relation.object_term_type,
  }))

  return { entities, relations }
}

function toNodeKeyPart(part: EtlMappingSummary['entities'][number]['key_parts'][number]): NodeKeyPart {
  if (part.kind === 'allocated_code') {
    return {
      kind: 'allocated_code',
      scopeColumns: [...part.scope_columns],
      rawValueColumn: part.raw_value_column,
    }
  }
  return { kind: 'column', column: part.column }
}
