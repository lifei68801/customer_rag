import { describe, expect, it } from 'vitest'
import { nextStepHint } from './nextStep'
import type { ModelingWorkspace, WorkspaceState } from './types'

const wrap = (state: Partial<WorkspaceState>): ModelingWorkspace => ({
  tenant_id: 't1',
  skill_name: 'consumer_retail',
  skill_version: '1',
  state: {
    term_types: [],
    relation_types: [],
    constraints: [],
    sources: [],
    unmatched_columns: {},
    questions: [],
    ...state,
  },
  updated_at: 'now',
  updated_by: 'alice',
})

const aTerm = (review: 'pending' | 'accepted' | 'rejected', dataMatch = false) => ({
  value: 'SKU',
  display_name: 'SKU',
  provenance: 'skill' as const,
  review,
  standard_name_value_type: 'string',
  extra_fields: [],
  key_aliases: [],
  field_aliases: {},
  clues: [],
  data_match: dataMatch
    ? { source_file: 'a.xls', key_columns: ['JAN'], field_columns: {}, matched_by: 'alias:jan' }
    : null,
})

describe('nextStepHint', () => {
  it('没有工作区时先建一个', () => {
    expect(nextStepHint(null, null)).toContain('选一个领域模板')
  })

  it('有未审的骨架元素时先审骨架', () => {
    expect(nextStepHint(wrap({ term_types: [aTerm('pending')] }), null)).toContain('审阅骨架')
  })

  it('骨架审完但没接数据时提示传表', () => {
    expect(nextStepHint(wrap({ term_types: [aTerm('accepted')] }), null)).toContain('上传数据表')
  })

  it('有 accepted 且接上数据时提示应用', () => {
    expect(nextStepHint(wrap({ term_types: [aTerm('accepted', true)] }), null)).toContain('应用到草稿')
  })

  it('diff 为空时说明已经应用过了', () => {
    const emptyDiff = {
      added_term_types: [],
      removed_term_types: [],
      changed_term_types: [],
      added_relation_types: [],
      removed_relation_types: [],
      added_constraints: [],
      removed_constraints: [],
    }
    expect(nextStepHint(wrap({ term_types: [aTerm('accepted', true)] }), emptyDiff)).toContain(
      '已经和草稿一致',
    )
  })
})
