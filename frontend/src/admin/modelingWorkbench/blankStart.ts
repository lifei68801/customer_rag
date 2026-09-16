import type { Proposal } from '../guidedOntology/types'
import type { WorkspaceConstraint, WorkspaceRelationType, WorkspaceTermType } from './types'

/**
 * 空白起步 + 只传表的退化路径（主 spec 前端一节承诺保留的那条）。
 *
 * 把 buildProposal 推出来的骨架翻成工作区元素。全部 provenance=data、review=
 * pending：这是"数据说这里可能有个概念"，不是用户的决定，用户仍在骨架面板逐条审。
 * 键别名取列名本身、字段别名取原列名（label）：下次同一客户再传同构的表能自动
 * 命中，跟手动指列的做法一致。
 */
export function proposalToWorkspace(
  proposal: Proposal,
  file: string,
): { term_types: WorkspaceTermType[]; relation_types: WorkspaceRelationType[]; constraints: WorkspaceConstraint[]; unmatched: string[] } {
  const term_types: WorkspaceTermType[] = proposal.termTypes.map((t) => {
    const fieldColumns = Object.fromEntries(t.extra_fields.map((f) => [f.name, f.label ?? f.name]))
    return {
      value: t.value,
      display_name: t.value,
      provenance: 'data',
      review: 'pending',
      standard_name_value_type: t.standard_name_value_type,
      extra_fields: t.extra_fields.map((f) => ({ ...f })),
      key_aliases: [t.value],
      field_aliases: Object.fromEntries(Object.entries(fieldColumns).map(([name, column]) => [name, [column]])),
      clues: [],
      data_match: { source_file: file, key_columns: [t.value], field_columns: fieldColumns, matched_by: 'column_role' },
    }
  })
  const relation_types: WorkspaceRelationType[] = proposal.relationTypes.map((r) => ({
    relation_type: r.relation_type,
    example_phrase: r.example_phrase,
    description: r.description,
    provenance: 'data',
    review: 'pending',
    clues: [],
    data_match: null,
  }))
  const constraints: WorkspaceConstraint[] = proposal.constraints.map((c) => ({
    subject: c.subject_term_type,
    relation: c.relation_type,
    object: c.object_term_type,
    provenance: 'data',
    review: 'pending',
  }))
  return { term_types, relation_types, constraints, unmatched: [...proposal.unusedColumns] }
}
