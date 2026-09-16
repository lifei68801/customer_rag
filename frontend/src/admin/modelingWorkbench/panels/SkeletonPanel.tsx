import type { Grounding, WorkspaceState } from '../types'
import { panelClass, secondaryButtonClass, tagClass } from '../ui'

const PROVENANCE_LABEL: Record<string, string> = {
  skill: '来自模板',
  data: '来自数据',
  manual: '手工新增',
}

const REVIEW_LABEL: Record<string, string> = {
  pending: '待审',
  accepted: '已接受',
  rejected: '已拒绝',
}

/**
 * 骨架审阅：每个元素一行，标出来源、落地与否、审阅状态。
 *
 * 拒绝的元素折叠在下面而不是消失（spec 行为规格 §2）：用户回头要能看到
 * "这个我拒过"，否则同一个概念会被反复提议、反复拒绝。
 */
export function SkeletonPanel(props: {
  state: WorkspaceState
  grounding: Grounding | null
  onReview: (kind: 'term' | 'relation', key: string, review: 'accepted' | 'rejected') => void
}) {
  const groundedTerms = new Set(props.grounding?.grounded_term_types ?? [])
  const groundedRelations = new Set(props.grounding?.grounded_relation_types ?? [])
  const terms = props.state.term_types
  const relations = props.state.relation_types

  return (
    <div className="flex flex-col gap-4">
      <section className={`${panelClass} flex flex-col gap-3`}>
        <h2 className="font-mono text-base font-semibold text-ink">实体类型</h2>
        {terms.length === 0 && <p className="text-sm text-ink-soft">还没有实体类型。</p>}
        {terms.map((term) => (
          <div key={term.value} className="flex flex-wrap items-center gap-2 border-b border-subtle pb-2">
            <span className="font-mono text-sm font-semibold text-ink">{term.value}</span>
            <span className="text-sm text-ink-soft">{term.display_name}</span>
            <span className={tagClass}>{PROVENANCE_LABEL[term.provenance]}</span>
            <span className={tagClass}>{REVIEW_LABEL[term.review]}</span>
            <span className={tagClass}>{groundedTerms.has(term.value) ? '已落地' : '未落地'}</span>
            {term.data_match && (
              <span className={tagClass}>
                {`${term.data_match.source_file} · ${term.data_match.key_columns.join('/')} · ${term.data_match.matched_by}`}
              </span>
            )}
            <button
              type="button"
              className={secondaryButtonClass}
              disabled={term.review === 'accepted'}
              onClick={() => props.onReview('term', term.value, 'accepted')}
            >
              {`接受 ${term.value}`}
            </button>
            <button
              type="button"
              className={secondaryButtonClass}
              disabled={term.review === 'rejected'}
              onClick={() => props.onReview('term', term.value, 'rejected')}
            >
              {`拒绝 ${term.value}`}
            </button>
          </div>
        ))}
      </section>

      <section className={`${panelClass} flex flex-col gap-3`}>
        <h2 className="font-mono text-base font-semibold text-ink">关系类型</h2>
        {relations.length === 0 && <p className="text-sm text-ink-soft">还没有关系类型。</p>}
        {relations.map((relation) => (
          <div
            key={relation.relation_type}
            className="flex flex-wrap items-center gap-2 border-b border-subtle pb-2"
          >
            <span className="font-mono text-sm font-semibold text-ink">{relation.relation_type}</span>
            <span className="text-sm text-ink-soft">{relation.example_phrase}</span>
            <span className={tagClass}>{PROVENANCE_LABEL[relation.provenance]}</span>
            <span className={tagClass}>{REVIEW_LABEL[relation.review]}</span>
            <span className={tagClass}>
              {groundedRelations.has(relation.relation_type) ? '已落地' : '未落地'}
            </span>
            <button
              type="button"
              className={secondaryButtonClass}
              disabled={relation.review === 'accepted'}
              onClick={() => props.onReview('relation', relation.relation_type, 'accepted')}
            >
              {`接受 ${relation.relation_type}`}
            </button>
            <button
              type="button"
              className={secondaryButtonClass}
              disabled={relation.review === 'rejected'}
              onClick={() => props.onReview('relation', relation.relation_type, 'rejected')}
            >
              {`拒绝 ${relation.relation_type}`}
            </button>
          </div>
        ))}
      </section>
    </div>
  )
}
