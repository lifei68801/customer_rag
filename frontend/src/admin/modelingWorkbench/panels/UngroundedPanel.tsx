import type { Grounding, WorkspaceState } from '../types'
import { panelClass, tagClass } from '../ui'

/**
 * 未落地清单：接受了、但 ETL 映射里没有任何一条指向它的元素。
 *
 * 这就是"下一批该接什么数据"的待办（spec 行为规格 §4）。不阻塞任何操作——
 * 未落地是一个合法的中间状态，企业的数据本来就是分批接进来的。
 */
export function UngroundedPanel(props: { state: WorkspaceState; grounding: Grounding | null }) {
  const groundedTerms = new Set(props.grounding?.grounded_term_types ?? [])
  const groundedRelations = new Set(props.grounding?.grounded_relation_types ?? [])
  const terms = props.state.term_types.filter(
    (t) => t.review !== 'rejected' && !groundedTerms.has(t.value),
  )
  const relations = props.state.relation_types.filter(
    (r) => r.review !== 'rejected' && !groundedRelations.has(r.relation_type),
  )

  return (
    <section className={`${panelClass} flex flex-col gap-3`}>
      <h2 className="font-mono text-base font-semibold text-ink">未落地清单</h2>
      {props.grounding?.parse_error && (
        <p role="alert" className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink">
          {`存下来的 ETL 映射解析失败，落地状态算不出来：${props.grounding.parse_error}`}
        </p>
      )}
      <p className="text-sm text-ink-soft">
        下面这些还没有数据支撑——ETL 映射里没有任何一条指向它们。这不是错误，是下一批该接的数据。
      </p>
      {terms.length === 0 && relations.length === 0 && (
        <p className="text-sm text-ink-soft">全都有数据支撑了。</p>
      )}
      {terms.map((term) => (
        <p key={term.value} className="text-sm text-ink">
          <span className="font-mono font-semibold">{term.value}</span>
          <span className={`ml-2 ${tagClass}`}>实体类型</span>
          {term.clues.map((clue, index) => (
            <span key={index} className={`ml-2 ${tagClass}`}>
              {`旁证：${clue.note}`}
            </span>
          ))}
        </p>
      ))}
      {relations.map((relation) => (
        <p key={relation.relation_type} className="text-sm text-ink">
          <span className="font-mono font-semibold">{relation.relation_type}</span>
          <span className={`ml-2 ${tagClass}`}>关系类型</span>
        </p>
      ))}
    </section>
  )
}
