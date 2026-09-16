import type { SourceParseOptions } from '../schemaEtlConfigBuilder/sourceParser'
import type { ColumnRole, InferredType, RoledColumn } from '../guidedOntology/types'

/** 这个元素是谁提出来的。v1 只会出现这三种（llm / document / question 是 v2）。 */
export type Provenance = 'skill' | 'data' | 'manual'

export type ReviewState = 'pending' | 'accepted' | 'rejected'

export interface WorkspaceExtraField {
  /** 内部名。应用到草稿时原样进 ontology_term_types.extra_fields。 */
  name: string
  value_type: string
  /** 显示名。可能是空串（后端 ExtraFieldSpec.label 允许为空）。 */
  label?: string
}

/** 数据发现时对上了哪张表的哪些列。只是历史记录——落地状态由 ETL 映射说了算。 */
export interface DataMatch {
  source_file: string
  key_columns: string[]
  field_columns: Record<string, string>
  /** 怎么对上的：`alias:<别名>` | `column_role` | `manual`。界面要说出依据。 */
  matched_by: string
}

/** 旁证。不改变落地状态，只用于排序和解释。v1 只有 manual 一种。 */
export interface Clue {
  kind: 'manual'
  note: string
  by: string
  at: string
}

export interface WorkspaceTermType {
  value: string
  display_name: string
  provenance: Provenance
  review: ReviewState
  standard_name_value_type: string
  extra_fields: WorkspaceExtraField[]
  key_aliases: string[]
  field_aliases: Record<string, string[]>
  clues: Clue[]
  data_match: DataMatch | null
}

export interface WorkspaceRelationType {
  relation_type: string
  example_phrase: string
  description: string
  provenance: Provenance
  review: ReviewState
  clues: Clue[]
  data_match: DataMatch | null
}

export interface WorkspaceConstraint {
  subject: string
  relation: string
  object: string
  provenance: Provenance
  review: ReviewState
}

/** 一条约束出不了关系映射时的说明。给应用面板显示，让用户去表格导入页手动配。 */
export interface SkippedRelation {
  subject: string
  relation: string
  object: string
  reason: string
}

/** 扫描时算出的一列：角色、判定依据、推断类型。存下来是为了不重扫就能显示依据、
 *  投影关系时判断主语表有没有宾语键列、手动指字段时推 value_type。 */
export interface SourceColumn {
  name: string
  role: ColumnRole
  /** 判定依据，必须带具体数字——用户要能据此推翻它（主 spec 对 reason 的要求）。 */
  reason: string
  inferred_type: InferredType
}

/** 一张表怎么读。跟 SourceParseOptions 同义，只是键名按后端 YAML 的写法。 */
export interface WorkspaceSource {
  file: string
  sheet?: string | number | null
  header_row?: number
  first_data_row?: number
  /** v1 存下的旧工作区没有这个键，视为"未知"。 */
  columns?: SourceColumn[]
}

export function columnsOf(roled: RoledColumn[]): SourceColumn[] {
  return roled.map((c) => ({
    name: c.stats.name,
    role: c.role,
    reason: c.reason,
    inferred_type: c.stats.inferredType,
  }))
}

export interface WorkspaceState {
  term_types: WorkspaceTermType[]
  relation_types: WorkspaceRelationType[]
  constraints: WorkspaceConstraint[]
  sources: WorkspaceSource[]
  /** 表名 -> 数据里有、骨架没接住的列。用户可以一键提升成新实体类型。 */
  unmatched_columns: Record<string, string[]>
  /** v2 的问题清单。v1 恒为空数组。 */
  questions: string[]
}

export interface ModelingWorkspace {
  tenant_id: string
  skill_name: string | null
  skill_version: string | null
  state: WorkspaceState
  updated_at: string
  updated_by: string
}

export interface SkillSummary {
  name: string
  version: string
  display_name: string
  description: string
  term_types: {
    value: string
    display_name: string
    standard_name_value_type: string
    extra_fields: { name: string; value_type: string; display_name: string }[]
    key_aliases: string[]
    field_aliases: Record<string, string[]>
  }[]
  relation_types: { relation_type: string; example_phrase: string; description: string }[]
  constraints: { subject: string; relation: string; object: string }[]
  questions: string[]
  match_hint: string
}

export interface Grounding {
  status: 'draft' | 'confirmed' | null
  grounded_term_types: string[]
  grounded_relation_types: string[]
  source_files: string[]
  /** 映射存在但解析失败时的原因。非空时三个列表是"算不出来"，不是"没有"。 */
  parse_error: string | null
}

export interface DraftDiff {
  added_term_types: string[]
  removed_term_types: string[]
  changed_term_types: string[]
  added_relation_types: string[]
  removed_relation_types: string[]
  added_constraints: string[]
  removed_constraints: string[]
}

/** `/draft/replace` 与 `apply-preview` 共用的提交形状。 */
export interface DraftPayload {
  term_types: { value: string; extra_fields: WorkspaceExtraField[]; standard_name_value_type: string }[]
  relation_types: {
    relation_type: string
    example_phrase: string
    description: string
    allow_chain_query: boolean
  }[]
  constraints: { subject_term_type: string; relation_type: string; object_term_type: string }[]
}

/** state 里的 sources 条目翻成读表用的解析选项。 */
export function parseOptionsOf(source: WorkspaceSource | undefined): SourceParseOptions {
  if (!source) return {}
  const options: SourceParseOptions = {}
  if (source.sheet !== undefined && source.sheet !== null) options.sheet = source.sheet
  if (source.header_row !== undefined) options.headerRow = source.header_row
  if (source.first_data_row !== undefined) options.firstDataRow = source.first_data_row
  return options
}
