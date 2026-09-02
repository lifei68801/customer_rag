export type InferredType = 'number' | 'integer' | 'date' | 'string'

export interface ColumnStats {
  name: string
  /** 非空值的行数。分母用它，不用总行数——90% 为空的列不该被当成低基数。 */
  nonEmptyCount: number
  /** 不同值的个数。封顶后等于 DISTINCT_LIMIT，见 distinctCapped。 */
  distinctCount: number
  /** 是否已封顶。封顶意味着"至少这么多"，不是精确值。 */
  distinctCapped: boolean
  /** 前几个不同值，给用户看。只给数字他判断不了。 */
  samples: string[]
  inferredType: InferredType
}

export type ColumnRole = 'identifier' | 'measure' | 'freetext' | 'date' | 'dimension'

export interface RoledColumn {
  stats: ColumnStats
  role: ColumnRole
  /** 判定依据。必须带具体数字——用户要能据此推翻它。 */
  reason: string
}

export interface DraftExtraField {
  name: string
  value_type: 'string' | 'number' | 'integer' | 'number[]'
}

export interface DraftTermType {
  value: string
  extra_fields: DraftExtraField[]
  standard_name_value_type: 'string'
}

export interface DraftRelationType {
  relation_type: string
  example_phrase: string
  description: string
  allow_chain_query: true
}

export interface DraftConstraint {
  subject_term_type: string
  relation_type: string
  object_term_type: string
}

export interface GuidedDecision {
  /** 每个维度列：建成实体类型（true）还是做成属性（false）。 */
  dimensionsAsEntity: Record<string, boolean>
  /** 每个非根实体挂在谁下面。键是实体名，值是父实体名。 */
  parentOf: Record<string, string>
  /** 每条边的关系类型名。键是子实体名。 */
  relationNameOf: Record<string, string>
}

export interface Proposal {
  termTypes: DraftTermType[]
  relationTypes: DraftRelationType[]
  constraints: DraftConstraint[]
  /** 没进本体的列。不显示等于静默丢弃。 */
  unusedColumns: string[]
  /** 属性名被清洗过的列：原列名 -> 清洗后的字段名。ETL 映射要用它对回去。 */
  renamedFields: Record<string, string>
  /** 没有标识列，根是猜的。UI 要提示用户确认。 */
  rootIsGuessed: boolean
}
