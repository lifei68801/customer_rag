export interface ExtraFieldSpec {
  name: string
  value_type: string
}

export interface ConfirmedTermType {
  value: string
  extra_fields: ExtraFieldSpec[]
}

export interface ConfirmedCombination {
  subject_term_type: string
  relation_type: string
  object_term_type: string
}

export interface AddedFile {
  id: string
  file: File
  columns: string[]
}

export interface ColumnKeyPart {
  kind: 'column'
  column: string
}

export interface AllocatedCodeKeyPart {
  kind: 'allocated_code'
  scopeColumns: string[]
  rawValueColumn: string
}

export type NodeKeyPart = ColumnKeyPart | AllocatedCodeKeyPart

export interface BuilderEntity {
  id: string
  termType: string
  fileId: string | null
  standardNameColumn: string
  nodeKeyParts: NodeKeyPart[]
  fieldMappings: Record<string, string>
}

export interface BuilderRelation {
  id: string
  fileId: string | null
  subjectTermType: string
  relationType: string
  objectTermType: string
}
