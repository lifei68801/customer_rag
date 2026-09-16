import type { WorkspaceState, WorkspaceTermType } from './types'

/**
 * 骨架审阅里的三个编辑动作。全是纯函数：输入非法（空名、重名）时**原样返回
 * 同一个对象引用**，调用方据此判断"什么都没变"并给出提示——抛异常的话每个
 * 调用点都要包 try，而这几种情况都不是异常，是用户打错字。
 */

function replaceTerm(
  state: WorkspaceState,
  value: string,
  update: (term: WorkspaceTermType) => WorkspaceTermType,
): WorkspaceState {
  return {
    ...state,
    term_types: state.term_types.map((t) => (t.value === value ? update(t) : t)),
  }
}

/**
 * 改名。约束里对它的引用一起改——不改的话投影时 `projectToDraftPayload` 的
 * 引用过滤会把那几条约束静默丢掉，用户只会发现"改了个名字，关系没了"。
 *
 * 别名不动：改的是这个概念叫什么，不是"怎么从列名认出它"。
 */
export function renameTermType(state: WorkspaceState, from: string, to: string): WorkspaceState {
  const trimmed = to.trim()
  if (trimmed === '' || trimmed === from) return state
  if (state.term_types.some((t) => t.value === trimmed)) return state
  if (!state.term_types.some((t) => t.value === from)) return state
  return {
    ...replaceTerm(state, from, (term) => ({ ...term, value: trimmed, display_name: trimmed })),
    constraints: state.constraints.map((c) => ({
      ...c,
      subject: c.subject === from ? trimmed : c.subject,
      object: c.object === from ? trimmed : c.object,
    })),
  }
}

/**
 * 加一条人工旁证（"我们有这个数据，下个月接"）。
 *
 * 旁证**不改变落地状态**（spec 决策 7：落地纯粹由 ETL 映射推导）。它只是让
 * 未落地清单上的这一条带着解释——否则三个月后没人记得为什么它还在清单上。
 */
export function addManualClue(
  state: WorkspaceState,
  termValue: string,
  note: string,
  by: string,
  at: string,
): WorkspaceState {
  const trimmed = note.trim()
  if (trimmed === '') return state
  if (!state.term_types.some((t) => t.value === termValue)) return state
  return replaceTerm(state, termValue, (term) => ({
    ...term,
    clues: [...term.clues, { kind: 'manual', note: trimmed, by, at }],
  }))
}

/**
 * 手工新增一个实体类型。直接 accepted——用户自己敲进去的东西不需要他再审
 * 一遍；pending 的语义是"有人/有东西提议了，等你看"。
 */
export function addManualTermType(state: WorkspaceState, value: string): WorkspaceState {
  const trimmed = value.trim()
  if (trimmed === '') return state
  if (state.term_types.some((t) => t.value === trimmed)) return state
  return {
    ...state,
    term_types: [
      ...state.term_types,
      {
        value: trimmed,
        display_name: trimmed,
        provenance: 'manual',
        review: 'accepted',
        standard_name_value_type: 'string',
        extra_fields: [],
        // 名字本身当别名：下次扫表时同名列还能对上
        key_aliases: [trimmed],
        field_aliases: {},
        clues: [],
        data_match: null,
      },
    ],
  }
}
