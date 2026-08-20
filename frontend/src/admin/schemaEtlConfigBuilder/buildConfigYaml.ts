import type { AddedFile, BuilderEntity, BuilderRelation } from './types'

// YAML 双引号字符串的标准转义：反斜杠、双引号、换行、制表符。所有本模块
// 生成的字符串标量一律走双引号风格，不用无引号 plain scalar——避免中文/
// 特殊字符（冒号、井号、前导连字符等）触发 YAML 语法歧义，不需要为每种
// 内容单独判断"要不要加引号"。
function yamlString(value: string): string {
  const escaped = value
    .replace(/\\/g, '\\\\')
    .replace(/"/g, '\\"')
    .replace(/\n/g, '\\n')
    .replace(/\t/g, '\\t')
  return `"${escaped}"`
}

function buildEntityYamlLines(entity: BuilderEntity, filenameById: Map<string, string>): string[] {
  const lines: string[] = []
  const sourceFile = filenameById.get(entity.fileId ?? '') ?? ''
  lines.push(`  - term_type: ${yamlString(entity.termType)}`)
  lines.push(`    source_file: ${yamlString(sourceFile)}`)
  lines.push(`    standard_name_column: ${yamlString(entity.standardNameColumn)}`)
  lines.push('    node_key_parts:')
  for (const part of entity.nodeKeyParts) {
    if (part.kind === 'column') {
      lines.push(`      - column: ${yamlString(part.column)}`)
    } else {
      lines.push('      - allocated_code:')
      lines.push('          scope_columns:')
      for (const col of part.scopeColumns) {
        lines.push(`            - ${yamlString(col)}`)
      }
      lines.push(`          raw_value_column: ${yamlString(part.rawValueColumn)}`)
    }
  }
  const fieldEntries = Object.entries(entity.fieldMappings)
  if (fieldEntries.length === 0) {
    lines.push('    field_mappings: {}')
  } else {
    lines.push('    field_mappings:')
    for (const [fieldName, sourceColumn] of fieldEntries) {
      lines.push(`      ${yamlString(fieldName)}: ${yamlString(sourceColumn)}`)
    }
  }
  return lines
}

function buildRelationYamlLines(relation: BuilderRelation, filenameById: Map<string, string>): string[] {
  const sourceFile = filenameById.get(relation.fileId ?? '') ?? ''
  return [
    `  - relation_type: ${yamlString(relation.relationType)}`,
    `    source_file: ${yamlString(sourceFile)}`,
    `    subject_term_type: ${yamlString(relation.subjectTermType)}`,
    `    object_term_type: ${yamlString(relation.objectTermType)}`,
  ]
}

export function buildConfigYaml(params: {
  tenantId: string
  entities: BuilderEntity[]
  relations: BuilderRelation[]
  files: AddedFile[]
}): string {
  const filenameById = new Map(params.files.map((f) => [f.id, f.file.name]))
  const lines: string[] = [`tenant_id: ${yamlString(params.tenantId)}`, '']

  if (params.entities.length === 0) {
    lines.push('entities: []')
  } else {
    lines.push('entities:')
    for (const entity of params.entities) {
      lines.push(...buildEntityYamlLines(entity, filenameById))
    }
  }

  lines.push('')

  if (params.relations.length === 0) {
    lines.push('relations: []')
  } else {
    lines.push('relations:')
    for (const relation of params.relations) {
      lines.push(...buildRelationYamlLines(relation, filenameById))
    }
  }

  return lines.join('\n') + '\n'
}
