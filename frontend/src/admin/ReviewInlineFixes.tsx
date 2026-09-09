import { useState } from 'react'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'
const buttonClass = `min-h-[36px] cursor-pointer rounded-control border border-subtle bg-paper px-3 text-sm font-bold text-ink transition hover:bg-interactive-hover disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`
const inputClass = `rounded-control border border-subtle bg-paper px-3 py-2 text-sm text-ink ${focusRing}`

/** 就地修复只需要待审记录里的这几列。 */
export interface FixableReview {
  review_id: number
  subject_candidate: string
  object_candidate: string
  relation_type: string
  reason: string
  subject_type_candidate: string | null
  object_type_candidate: string | null
}

/** 缺的是哪一端，由 reason 决定。 */
export function missingSide(reason: string): 'subject' | 'object' {
  return reason === 'object_unresolved' ? 'object' : 'subject'
}

/**
 * 「一端对不上」那一页的就地修复：建出缺的实体，然后批准这条待审。
 *
 * 此前审核员得跳到实体明细页建实体、再回来找到这条待审批准——中间隔着一次
 * 导航和一次搜索，而他手上正开着十几条。
 */
export function InlineCreateMissingTerm({
  review,
  termTypeOptions,
  busy,
  error,
  onSubmit,
}: {
  review: FixableReview
  termTypeOptions: string[]
  busy: boolean
  error?: string
  onSubmit: (termType: string) => void
}) {
  const side = missingSide(review.reason)
  const candidate = side === 'object' ? review.object_candidate : review.subject_candidate
  // 类型预填管线猜的那个（如果它恰好是本体里已有的）。猜错了审核员改一下就行，
  // 而大多数时候它是对的——让他每条都从头选一遍是在收一笔没必要的税。
  const guessed = side === 'object' ? review.object_type_candidate : review.subject_type_candidate
  const [termType, setTermType] = useState(
    guessed && termTypeOptions.includes(guessed) ? guessed : '',
  )

  return (
    <div className="flex flex-col gap-2 rounded-card border border-subtle bg-paper p-3">
      <p className="text-xs text-ink-soft">
        {side === 'subject' ? '主语' : '宾语'}这一端在术语表里找不到。就地建出来并批准：
      </p>
      <div className="flex flex-wrap items-center gap-2">
        {/* 名字预填候选名——让审核员从头敲一遍等于给他一次敲错的机会。
            但仍然可改：管线抽出来的名字带错别字是常事。 */}
        <input
          aria-label="要新建的实体名"
          className={`${inputClass} flex-1`}
          defaultValue={candidate}
          readOnly
        />
        <select
          aria-label="实体类型"
          className={inputClass}
          value={termType}
          onChange={(event) => setTermType(event.target.value)}
        >
          <option value="">选类型…</option>
          {termTypeOptions.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
        <button
          type="button"
          className={buttonClass}
          disabled={busy || !termType}
          onClick={() => onSubmit(termType)}
        >
          新建并批准
        </button>
      </div>
      {/* 按钮点不动且不说原因的话，用户会以为界面坏了。 */}
      {!termType && <p className="text-xs text-ink-soft">先选一个类型才能建。</p>}
      {error && (
        <p role="alert" className="text-xs text-status-error-strong">
          {error}
        </p>
      )}
    </div>
  )
}

/**
 * 「不在本体」那一页的就地修复：把这条的类型组合加进本体草稿。
 *
 * **按钮上写出具体组合**，不是「加白名单」——审核员要知道自己在放宽什么。
 *
 * 措辞是「加进草稿」而不是「并批准」：后端只把组合写进 status='draft'，
 * 这条待审仍留在队列里（加完立刻批准会撞
 * RelationNotInConfirmedOntologyError，而唯一能一步到位的 confirm_ontology
 * 会把整份草稿——包括别人正在编辑的半成品——一起发布出去）。写成"并批准"
 * 就是在界面上承诺一件没发生的事。
 */
export function InlineAllowCombination({
  review,
  busy,
  error,
  note,
  onSubmit,
}: {
  review: FixableReview
  busy: boolean
  error?: string
  note?: string
  onSubmit: () => void
}) {
  const subjectType = review.subject_type_candidate
  const objectType = review.object_type_candidate

  if (!subjectType || !objectType) {
    // 两端类型没识别出来时不给按钮：加进去的会是一个带空类型的组合，它匹配
    // 不上任何东西——白名单里多一条永远不生效的规则，而用户以为自己放宽了
    // 本体。说清楚该去哪儿做，而不是给一个点了就报错的按钮。
    return (
      <p className="text-xs text-ink-soft">
        这条没识别出两端的类型，没法就地加白名单。先在「一端对不上」那一页把缺的实体
        建出来（建的时候要选类型），或者直接去本体结构页手工加这条组合。
      </p>
    )
  }

  return (
    <div className="flex flex-col gap-2">
      <button
        type="button"
        className={buttonClass}
        disabled={busy}
        onClick={onSubmit}
      >
        把 {subjectType} —[{review.relation_type}]→ {objectType} 加进本体草稿
      </button>
      {note && (
        <p role="status" className="text-xs text-ink-soft">
          {note}
        </p>
      )}
      {error && (
        <p role="alert" className="text-xs text-status-error-strong">
          {error}
        </p>
      )}
    </div>
  )
}
