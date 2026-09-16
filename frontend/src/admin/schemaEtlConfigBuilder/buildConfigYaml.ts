import type { SourceParseOptions } from './sourceParser'
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

function hasNonDefaultParseOptions(options: SourceParseOptions): boolean {
  return (
    options.sheet !== undefined ||
    (options.headerRow !== undefined && options.headerRow !== 1) ||
    options.firstDataRow !== undefined
  )
}

export function buildConfigYaml(params: {
  tenantId: string
  entities: BuilderEntity[]
  relations: BuilderRelation[]
  files: AddedFile[]
}): string {
  const filenameById = new Map(params.files.map((f) => [f.id, f.file.name]))
  const lines: string[] = [`tenant_id: ${yamlString(params.tenantId)}`, '']

  // 只写用户真正改过的那些。全是缺省值也写一遍，等于把"第 1 行"固化进配置；
  // 将来缺省变了，这些配置不会跟着变，而用户从没做过这个选择。
  const configured = params.files.filter((f) => hasNonDefaultParseOptions(f.parseOptions))
  if (configured.length > 0) {
    lines.push('sources:')
    for (const f of configured) {
      lines.push(`  - file: ${yamlString(f.file.name)}`)
      const { sheet, headerRow, firstDataRow } = f.parseOptions
      if (sheet !== undefined) {
        // 序号是数字标量，名字是字符串标量——写成 sheet: "1" 的话后端会把它
        // 当成一张名叫 "1" 的工作表去找，找不到就报错。
        lines.push(`    sheet: ${typeof sheet === 'number' ? sheet : yamlString(sheet)}`)
      }
      if (headerRow !== undefined) lines.push(`    header_row: ${headerRow}`)
      if (firstDataRow !== undefined) lines.push(`    first_data_row: ${firstDataRow}`)
    }
    lines.push('')
  }

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
