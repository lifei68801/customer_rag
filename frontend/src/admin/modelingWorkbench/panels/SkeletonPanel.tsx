import { useState } from 'react'
import { parseAliasText } from '../columnAssign'
import { matchedByLabel } from '../types'
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
  busy: boolean
  onReview: (kind: 'term' | 'relation', key: string, review: 'accepted' | 'rejected') => void
  onRename: (from: string, to: string) => void
  onAddClue: (termValue: string, note: string) => void
  onAddTerm: (value: string) => void
  onSetKeyAliases: (termValue: string, aliases: string[]) => void
  onSetFieldAliases: (termValue: string, fieldName: string, aliases: string[]) => void
}) {
  const [renameDraft, setRenameDraft] = useState<Record<string, string>>({})
  const [clueDraft, setClueDraft] = useState<Record<string, string>>({})
  const [newTerm, setNewTerm] = useState('')

  /** 保存成功后丢掉这一条草稿，让输入框回到"显示工作区里的当前值"。
   *  不丢的话，别处（重新扫描、另一个人的改动）更新了别名，这个框还显示着
   *  旧草稿，再点一次保存就把它整份写回去了。加旁证那两个框也是保存即清。 */
  const dropAliasDraft = (key: string) => {
    setAliasDraft((current) => {
      const next = { ...current }
      delete next[key]
      return next
    })
  }
  const [aliasDraft, setAliasDraft] = useState<Record<string, string>>({})
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
                {`${term.data_match.source_file} · ${term.data_match.key_columns.join('/')} · ${matchedByLabel(term.data_match.matched_by)}`}
              </span>
            )}
            <button
              type="button"
              className={secondaryButtonClass}
              disabled={props.busy || term.review === 'accepted'}
              onClick={() => props.onReview('term', term.value, 'accepted')}
            >
              {`接受 ${term.value}`}
            </button>
            <button
              type="button"
              className={secondaryButtonClass}
              disabled={props.busy || term.review === 'rejected'}
              onClick={() => props.onReview('term', term.value, 'rejected')}
            >
              {`拒绝 ${term.value}`}
            </button>
            <label className="sr-only" htmlFor={`rename-${term.value}`}>
              {`${term.value} 的新名字`}
            </label>
            <input
              id={`rename-${term.value}`}
              className="w-32 rounded-control border border-subtle bg-paper px-2 py-1 text-sm"
              value={renameDraft[term.value] ?? term.value}
              onChange={(e) => setRenameDraft({ ...renameDraft, [term.value]: e.target.value })}
            />
            <button
              type="button"
              className={secondaryButtonClass}
              disabled={props.busy}
              onClick={() => props.onRename(term.value, renameDraft[term.value] ?? term.value)}
            >
              {`改名 ${term.value}`}
            </button>
            <label className="sr-only" htmlFor={`clue-${term.value}`}>
              {`给 ${term.value} 加旁证`}
            </label>
            <input
              id={`clue-${term.value}`}
              className="w-40 rounded-control border border-subtle bg-paper px-2 py-1 text-sm"
              placeholder="例如：数据下个月接"
              value={clueDraft[term.value] ?? ''}
              onChange={(e) => setClueDraft({ ...clueDraft, [term.value]: e.target.value })}
            />
            <button
              type="button"
              className={secondaryButtonClass}
              disabled={props.busy}
              onClick={() => {
                props.onAddClue(term.value, clueDraft[term.value] ?? '')
                setClueDraft({ ...clueDraft, [term.value]: '' })
              }}
            >
              {`加旁证 ${term.value}`}
            </button>
            <div className="flex w-full flex-wrap items-center gap-2 pl-4">
              <label className="sr-only" htmlFor={`alias-key-${term.value}`}>{`${term.value} 的键别名`}</label>
              <input
                id={`alias-key-${term.value}`}
                className="w-64 rounded-control border border-subtle bg-paper px-2 py-1 text-sm"
                placeholder="键别名，逗号分隔"
                value={aliasDraft[`key:${term.value}`] ?? term.key_aliases.join(', ')}
                onChange={(e) => setAliasDraft({ ...aliasDraft, [`key:${term.value}`]: e.target.value })}
              />
              <button
                type="button"
                className={secondaryButtonClass}
                disabled={props.busy}
                onClick={() => {
                  props.onSetKeyAliases(
                    term.value,
                    parseAliasText(aliasDraft[`key:${term.value}`] ?? term.key_aliases.join(', ')),
                  )
                  dropAliasDraft(`key:${term.value}`)
                }}
              >
                {`保存 ${term.value} 的键别名`}
              </button>
              {term.extra_fields.map((field) => {
                const draftKey = `field:${term.value}:${field.name}`
                const current = (term.field_aliases[field.name] ?? []).join(', ')
                return (
                  <span key={field.name} className="flex items-center gap-2">
                    <label className="sr-only" htmlFor={`alias-${draftKey}`}>
                      {`${term.value} 的字段 ${field.name} 的别名`}
                    </label>
                    <input
                      id={`alias-${draftKey}`}
                      className="w-48 rounded-control border border-subtle bg-paper px-2 py-1 text-sm"
                      placeholder={`${field.label || field.name} 的别名`}
                      value={aliasDraft[draftKey] ?? current}
                      onChange={(e) => setAliasDraft({ ...aliasDraft, [draftKey]: e.target.value })}
                    />
                    <button
                      type="button"
                      className={secondaryButtonClass}
                      disabled={props.busy}
                      onClick={() => {
                        props.onSetFieldAliases(term.value, field.name, parseAliasText(aliasDraft[draftKey] ?? current))
                        dropAliasDraft(draftKey)
                      }}
                    >
                      {`保存 ${term.value} 的字段 ${field.name} 的别名`}
                    </button>
                  </span>
                )
              })}
              <span className="text-xs text-ink-soft">别名改了要重新扫描数据表才生效。</span>
            </div>
          </div>
        ))}
        <div className="flex flex-wrap items-center gap-2">
          <label className="sr-only" htmlFor="new-term">
            新实体类型名
          </label>
          <input
            id="new-term"
            className="w-40 rounded-control border border-subtle bg-paper px-2 py-1 text-sm"
            placeholder="骨架里缺的概念"
            value={newTerm}
            onChange={(e) => setNewTerm(e.target.value)}
          />
          <button
            type="button"
            className={secondaryButtonClass}
            disabled={props.busy}
            onClick={() => {
              props.onAddTerm(newTerm)
              setNewTerm('')
            }}
          >
            新增实体类型
          </button>
        </div>
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
              disabled={props.busy || relation.review === 'accepted'}
              onClick={() => props.onReview('relation', relation.relation_type, 'accepted')}
            >
              {`接受 ${relation.relation_type}`}
            </button>
            <button
              type="button"
              className={secondaryButtonClass}
              disabled={props.busy || relation.review === 'rejected'}
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
