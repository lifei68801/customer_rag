import type { InterviewState, ReviewState, SmartConstraint } from './types'
import type { DraftPayload } from './types'

/**
 * 智能创建骨架审阅里的编辑动作。全是纯函数：输入非法（重名、找不到）时
 * **原样返回同一个对象引用**，调用方据此判断"什么都没变"，不用到处包 try——
 * 这些都不是异常，是用户点了一个此刻做不到的操作。
 *
 * 跟 `modelingWorkbench/skeletonEdits.ts` 是同一种写法，故意不 import：三种
 * 构建方式各自独立（spec 决策 2）。
 */

/** 约束没有单一名字字段，审阅时用"主语|关系|宾语"拼一个 key。 */
export function constraintKey(c: { subject: string; relation: string; object: string }): string {
  return `${c.subject}|${c.relation}|${c.object}`
}

/**
 * 改一条元素的审阅状态。kind 决定去哪个数组里找、按什么字段匹配 key：
 * term 按 value，relation 按 relation_type，constraint 按 constraintKey。
 */
export function setReview(
  state: InterviewState,
  kind: 'term' | 'relation' | 'constraint',
  key: string,
  review: ReviewState,
): InterviewState {
  const skeleton = state.skeleton
  if (kind === 'term') {
    if (!skeleton.term_types.some((t) => t.value === key)) return state
    return {
      ...state,
      skeleton: {
        ...skeleton,
        term_types: skeleton.term_types.map((t) => (t.value === key ? { ...t, review } : t)),
      },
    }
  }
  if (kind === 'relation') {
    if (!skeleton.relation_types.some((r) => r.relation_type === key)) return state
    return {
      ...state,
      skeleton: {
        ...skeleton,
        relation_types: skeleton.relation_types.map((r) =>
          r.relation_type === key ? { ...r, review } : r,
        ),
      },
    }
  }
  if (!skeleton.constraints.some((c) => constraintKey(c) === key)) return state
  return {
    ...state,
    skeleton: {
      ...skeleton,
      constraints: skeleton.constraints.map((c) => (constraintKey(c) === key ? { ...c, review } : c)),
    },
  }
}

/**
 * 问题清单里"缺的名字"一键加进骨架——加成实体类型。
 *
 * 直接 `pending`（不是 `accepted`）：这是从一个问题反推出来的猜测，跟访谈里
 * LLM 猜的元素同一个信任级别，用户还是要审一遍。`rationale` 写明来源——
 * "来自问题：xxx"——不然三个月后没人记得这条实体是哪来的。
 *
 * 重名（不区分是不是同一批加的）原样返回：骨架里已经有同名元素，用户要做的
 * 是去审那一条，不是再造一条重复的。
 */
export function addMissingAsTerm(
  state: InterviewState,
  name: string,
  questionText: string,
): InterviewState {
  const trimmed = name.trim()
  if (trimmed === '') return state
  if (state.skeleton.term_types.some((t) => t.value === trimmed)) return state
  return {
    ...state,
    skeleton: {
      ...state.skeleton,
      term_types: [
        ...state.skeleton.term_types,
        {
          value: trimmed,
          display_name: trimmed,
          rationale: `来自问题：${questionText}`,
          confidence: 'guess',
          from_turn: Math.max(state.turns.length - 1, 0),
          review: 'pending',
          extra_fields: [],
          standard_name_value_type: 'string',
        },
      ],
    },
  }
}

/** 同 addMissingAsTerm，加成关系类型（全大写下划线命名的那些）。 */
export function addMissingAsRelation(
  state: InterviewState,
  name: string,
  questionText: string,
): InterviewState {
  const trimmed = name.trim()
  if (trimmed === '') return state
  if (state.skeleton.relation_types.some((r) => r.relation_type === trimmed)) return state
  return {
    ...state,
    skeleton: {
      ...state.skeleton,
      relation_types: [
        ...state.skeleton.relation_types,
        {
          relation_type: trimmed,
          example_phrase: '',
          description: '',
          rationale: `来自问题：${questionText}`,
          confidence: 'guess',
          from_turn: Math.max(state.turns.length - 1, 0),
          review: 'pending',
        },
      ],
    },
  }
}

/**
 * 骨架 → `/draft/replace` 的 payload。只投影 `accepted` 的元素——pending 是
 * "还没看"，rejected 是"看过不要"，两者都不该进本体。
 *
 * 约束这里比模板构建更严格：模板构建允许 relation 是 pending（骨架面板没有
 * 约束区块，去留由主宾两端决定），但智能创建的骨架面板**有**约束区块、约束
 * 本身也能被单独接受/拒绝（brief 的"骨架"三节之一），所以这里要求主语、宾语、
 * 关系三者都 accepted 才带上约束——跟简报"约束三者都 accepted 才带"一致。
 */
export function projectSkeleton(state: InterviewState): DraftPayload {
  const terms = state.skeleton.term_types.filter((t) => t.review === 'accepted')
  const relations = state.skeleton.relation_types.filter((r) => r.review === 'accepted')
  const termValues = new Set(terms.map((t) => t.value))
  const relationNames = new Set(relations.map((r) => r.relation_type))
  const constraintAccepted = (c: SmartConstraint) =>
    c.review === 'accepted' &&
    termValues.has(c.subject) &&
    termValues.has(c.object) &&
    relationNames.has(c.relation)
  return {
    term_types: terms.map((t) => ({
      value: t.value,
      extra_fields: t.extra_fields.map((f) => ({ ...f })),
      standard_name_value_type: t.standard_name_value_type,
    })),
    relation_types: relations.map((r) => ({
      relation_type: r.relation_type,
      example_phrase: r.example_phrase,
      description: r.description,
      allow_chain_query: true,
    })),
    constraints: state.skeleton.constraints.filter(constraintAccepted).map((c) => ({
      subject_term_type: c.subject,
      relation_type: c.relation,
      object_term_type: c.object,
    })),
  }
}
