import { sanitizeFieldName } from '../guidedOntology/draftProposal'
import type { InferredType } from '../guidedOntology/types'
import type { WorkspaceState, WorkspaceTermType } from './types'

/**
 * 手动指列与别名编辑。全是纯函数：输入非法时**原样返回同一引用**，调用方据此
 * 判断"什么都没变"并提示——跟 skeletonEdits.ts 同一约定。
 *
 * 这是主 spec 行为规格 §3 说的"命不中的代价是用户手动指一下列"的那个入口。
 * 指完把列名写进别名，下一次同一客户的表就能自动命中——手动一次，规则长期有效。
 */

function replaceTerm(
  state: WorkspaceState,
  value: string,
  update: (term: WorkspaceTermType) => WorkspaceTermType,
): WorkspaceState {
  return { ...state, term_types: state.term_types.map((t) => (t.value === value ? update(t) : t)) }
}

function findActive(state: WorkspaceState, termValue: string): WorkspaceTermType | undefined {
  const term = state.term_types.find((t) => t.value === termValue)
  return term && term.review !== 'rejected' ? term : undefined
}

function removeUnmatched(state: WorkspaceState, file: string, column: string): Record<string, string[]> {
  return {
    ...state.unmatched_columns,
    [file]: (state.unmatched_columns[file] ?? []).filter((name) => name !== column),
  }
}

function moveToFront(list: string[], item: string): string[] {
  return [item, ...list.filter((a) => a !== item)]
}

/**
 * 把一列指给实体当键列。整体替换 data_match 的键，但同一张表上已经对上的字段
 * 保留——用户只是换了键列，没说要放弃字段；换表则字段全部作废（它们是另一张
 * 表的列）。
 */
export function assignColumnAsKey(
  state: WorkspaceState,
  file: string,
  column: string,
  termValue: string,
): WorkspaceState {
  const term = findActive(state, termValue)
  if (!term) return state
  const sameFile = term.data_match?.source_file === file
  return {
    ...replaceTerm(state, termValue, (t) => ({
      ...t,
      // 手动指的别名放最前，不是追加在末尾：alignTable 按 key_aliases 顺序
      // 取第一个命中的别名，追加在末尾会让旧别名（比如碰巧先命中另一列的
      // jan）在下次重扫时抢先命中，把这次手动纠正的 data_match 静默翻回去，
      // 直接违背"指完一次，下次自动命中"。
      key_aliases: moveToFront(t.key_aliases, column),
      data_match: {
        source_file: file,
        key_columns: [column],
        field_columns: sameFile ? { ...t.data_match!.field_columns } : {},
        matched_by: 'manual',
      },
    })),
    unmatched_columns: removeUnmatched(state, file, column),
  }
}

function valueTypeOf(inferred: InferredType | undefined): string {
  if (inferred === 'integer' || inferred === 'number' || inferred === 'date') return inferred
  return 'string'
}

function uniqueFieldName(base: string, taken: Set<string>): string {
  if (!taken.has(base)) return base
  let n = 2
  while (taken.has(`${base}_${n}`)) n += 1
  return `${base}_${n}`
}

/**
 * 把一列指给实体当字段。要求实体已经在**同一张表**有键列：字段是"这一行的
 * 属性"，没有键就不知道这一行是谁的。
 */
export function assignColumnAsField(
  state: WorkspaceState,
  file: string,
  column: string,
  termValue: string,
): WorkspaceState {
  const term = findActive(state, termValue)
  if (!term || !term.data_match || term.data_match.source_file !== file) return state
  const source = state.sources.find((s) => s.file === file)
  const columnIndex = source?.columns?.findIndex((c) => c.name === column) ?? -1
  const inferred = columnIndex >= 0 ? source!.columns![columnIndex].inferred_type : undefined
  const taken = new Set(term.extra_fields.map((f) => f.name))
  const fieldName = uniqueFieldName(sanitizeFieldName(column, Math.max(columnIndex, 0)), taken)
  return {
    ...replaceTerm(state, termValue, (t) => ({
      ...t,
      // 显示名记原列名：清洗后的内部名对中文列名只是 field_N 这种占位。
      extra_fields: [...t.extra_fields, { name: fieldName, value_type: valueTypeOf(inferred), label: column }],
      field_aliases: { ...t.field_aliases, [fieldName]: [column] },
      data_match: {
        ...t.data_match!,
        field_columns: { ...t.data_match!.field_columns, [fieldName]: column },
      },
    })),
    unmatched_columns: removeUnmatched(state, file, column),
  }
}

export function parseAliasText(text: string): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const raw of text.split(/[,，]/)) {
    const alias = raw.trim()
    if (alias === '' || seen.has(alias)) continue
    seen.add(alias)
    out.push(alias)
  }
  return out
}

export function setKeyAliases(state: WorkspaceState, termValue: string, aliases: string[]): WorkspaceState {
  if (!state.term_types.some((t) => t.value === termValue)) return state
  return replaceTerm(state, termValue, (t) => ({ ...t, key_aliases: [...aliases] }))
}

export function setFieldAliases(
  state: WorkspaceState,
  termValue: string,
  fieldName: string,
  aliases: string[],
): WorkspaceState {
  const term = state.term_types.find((t) => t.value === termValue)
  if (!term || !term.extra_fields.some((f) => f.name === fieldName)) return state
  return replaceTerm(state, termValue, (t) => ({
    ...t,
    field_aliases: { ...t.field_aliases, [fieldName]: [...aliases] },
  }))
}
