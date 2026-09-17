/**
 * 智能创建（访谈式建模）的类型。跟 `modelingWorkbench/types.ts` 形状相似但
 * 故意不共用——两者是同一个「元素」概念在两个独立子系统里的投影，spec
 * 决策 2 要求三种构建方式不共享状态/类型，免得改一处牵动另一处。
 */

/** v1 只有一个取值——全都是猜的。留着这个字段是为了将来区分"用户明确说过
 *  的"与"LLM 推的"。 */
export type Confidence = 'guess'

export type ReviewState = 'pending' | 'accepted' | 'rejected'

export interface InterviewTurn {
  role: 'assistant' | 'user'
  text: string
}

export interface ExtraField {
  name: string
  value_type: string
  label?: string
}

export interface SmartTermType {
  value: string
  display_name: string
  /** 必填：一条用户没说过的实体凭空出现而不给理由，用户没法判断该不该留。 */
  rationale: string
  confidence: Confidence
  /** turns 的下标，界面上点一下能跳回那一轮对话。 */
  from_turn: number
  review: ReviewState
  extra_fields: ExtraField[]
  standard_name_value_type: string
}

export interface SmartRelationType {
  relation_type: string
  example_phrase: string
  description: string
  rationale: string
  confidence: Confidence
  from_turn: number
  review: ReviewState
}

export interface SmartConstraint {
  subject: string
  relation: string
  object: string
  rationale: string
  confidence: Confidence
  from_turn: number
  review: ReviewState
}

export interface SmartSkeleton {
  term_types: SmartTermType[]
  relation_types: SmartRelationType[]
  constraints: SmartConstraint[]
}

export interface InterviewQuestionNeeds {
  term_types: string[]
  relation_types: string[]
}

export interface InterviewQuestion {
  text: string
  needs: InterviewQuestionNeeds
  /** needs 里不在骨架（review != 'rejected'）中的名字，后端算好的，前端不重算。 */
  missing: string[]
  at: string
}

export interface InterviewState {
  turns: InterviewTurn[]
  skeleton: SmartSkeleton
  questions: InterviewQuestion[]
  done: boolean
}

export interface InterviewSession {
  tenant_id: string
  state: InterviewState
  updated_at: string
  updated_by: string
}

/** 回答一轮之后的反馈：新一个问题、新增了几条、丢了几条、失败时的说明。
 *  dropped 是后端给的可读理由列表（如"实体类型 X 没有给出理由，丢弃"），
 *  不是计数——理由本身才是用户要看的，不是"丢了几条"这个数字。 */
export interface TurnReport {
  question: string | null
  added_count: number
  dropped: string[]
  note: string | null
}

/** `/draft/replace` 与 `apply-preview` 共用的提交形状。这里自己声明一份，
 *  不 import `modelingWorkbench/types.ts` 的 DraftPayload（spec 决策 2）。 */
export interface DraftPayload {
  term_types: { value: string; extra_fields: ExtraField[]; standard_name_value_type: string }[]
  relation_types: {
    relation_type: string
    example_phrase: string
    description: string
    allow_chain_query: boolean
  }[]
  constraints: { subject_term_type: string; relation_type: string; object_term_type: string }[]
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
