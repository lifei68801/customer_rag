/**
 * 本体数据的形状。
 *
 * 从 OntologySchemaPage 里搬出来的：本体图变成独立页面之后，页面和取数
 * hook 都要用同一批类型，留在其中一方会形成循环引用。
 */

export type ViewMode = 'draft' | 'confirmed'

export interface ExtraFieldSpec {
  name: string
  value_type: string
}

export interface TermType {
  value: string
  extra_fields: ExtraFieldSpec[]
  standard_name_value_type: string
}

export interface RelationType {
  relation_type: string
  example_phrase: string
  description: string
  allow_chain_query: boolean
}

export interface Constraint {
  subject_term_type: string
  relation_type: string
  object_term_type: string
}
