import { buildConfigYaml } from '../schemaEtlConfigBuilder/buildConfigYaml'
import type { AddedFile, BuilderEntity, BuilderRelation } from '../schemaEtlConfigBuilder/types'
import { parseOptionsOf } from './types'
import type { DraftPayload, WorkspaceState } from './types'

/**
 * 工作区 → `/draft/replace` 的 payload。
 *
 * 只取 review === 'accepted' 的元素：pending 是"还没看"，rejected 是"看过不要"，
 * 两者都不该进本体。拒掉的元素**留在工作区里**（spec 决策 2），这里只是不投影。
 *
 * 约束做引用过滤而不是原样带过去：replace_draft 会对引用未声明类型的约束抛
 * UnknownCategoryError，整次应用失败，而用户看到的只是一句"引用了未声明的
 * 实体类型"——他并不知道是自己哪一次拒绝造成的。
 */
export function projectToDraftPayload(state: WorkspaceState): DraftPayload {
  const terms = state.term_types.filter((t) => t.review === 'accepted')
  const relations = state.relation_types.filter((r) => r.review === 'accepted')
  const termValues = new Set(terms.map((t) => t.value))
  const relationNames = new Set(relations.map((r) => r.relation_type))
  return {
    term_types: terms.map((t) => ({
      value: t.value,
      extra_fields: t.extra_fields.map((f) => ({ ...f })),
      standard_name_value_type: t.standard_name_value_type,
    })),
    relation_types: relations.map((r) => ({
      relation_type: r.relation_type,
      example_phrase: r.example_phrase,
      description: r.description,
      allow_chain_query: true,
    })),
    constraints: state.constraints
      .filter(
        (c) =>
          c.review === 'accepted' &&
          termValues.has(c.subject) &&
          termValues.has(c.object) &&
          relationNames.has(c.relation),
      )
      .map((c) => ({
        subject_term_type: c.subject,
        relation_type: c.relation,
        object_term_type: c.object,
      })),
  }
}

/**
 * 工作区 → ETL 映射 YAML。没有任何实体接上了数据时返回 null——那时这次应用
 * 不带映射，replace_draft 会保留已有的那份（见它的 docstring）。
 *
 * 复用 buildConfigYaml 而不是自己拼 YAML：它已经处理了引号转义、sources 段、
 * allocated_code 这些细节，而且表格导入页用的就是它，两条路径产出的 YAML
 * 形状一致，后端只需要认一种。
 *
 * 它要一个 AddedFile（含真正的 File 对象）才能拿到文件名。工作台这里只有
 * 文件名——用户可能是上一次会话传的表，File 对象早没了。所以构造一个空的
 * `new File([], name)`：buildConfigYaml 只读 `file.name`，不读内容。
 */
export function projectToEtlYaml(
  state: WorkspaceState,
  tenantId: string,
): { yaml: string; fileName: string } | null {
  const matched = state.term_types.filter((t) => t.review === 'accepted' && t.data_match !== null)
  if (matched.length === 0) return null

  const fileNames = [...new Set(matched.map((t) => t.data_match!.source_file))]
  const files: AddedFile[] = fileNames.map((name) => ({
    id: name,
    file: new File([], name),
    columns: [],
    parseOptions: parseOptionsOf(state.sources.find((s) => s.file === name)),
  }))

  const entities: BuilderEntity[] = matched.map((term) => {
    const match = term.data_match!
    return {
      id: term.value,
      termType: term.value,
      fileId: match.source_file,
      // 标准名暂用键列：工作台没有"显示名取哪一列"的判断依据，而键列一定
      // 存在。用户可以在表格导入页改。
      standardNameColumn: match.key_columns[0],
      nodeKeyParts: match.key_columns.map((column) => ({ kind: 'column' as const, column })),
      fieldMappings: { ...match.field_columns },
    }
  })

  const relations: BuilderRelation[] = state.constraints
    .filter(
      (c) =>
        c.review === 'accepted' &&
        matched.some((t) => t.value === c.subject) &&
        matched.some((t) => t.value === c.object),
    )
    .map((c) => ({
      id: `${c.subject}-${c.relation}-${c.object}`,
      // 关系从主语所在的那张表出：那张表的每一行都指向一个宾语。
      fileId: matched.find((t) => t.value === c.subject)!.data_match!.source_file,
      subjectTermType: c.subject,
      relationType: c.relation,
      objectTermType: c.object,
    }))

  return {
    yaml: buildConfigYaml({ tenantId, entities, relations, files }),
    fileName: fileNames[0],
  }
}
