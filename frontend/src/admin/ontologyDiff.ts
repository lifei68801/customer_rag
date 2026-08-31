/**
 * 草稿与已确认版本的差异计算。
 *
 * 为什么需要：确认（confirm）是不可逆的——旧的已确认版本会被换掉、无法
 * 恢复（见 ontology_lifecycle.confirm_ontology）。而本体页此前只有"看草稿"
 * 和"看已确认"两个**视图**，看不到两者的**差异**。用户在按下一个不可逆
 * 按钮之前，应该能看到这次到底要改什么。
 *
 * 纯函数、不碰网络：三个接口都已支持 ?status=draft|confirmed，两边各拉一次
 * 在前端比对即可，不需要后端新增能力。
 */

export interface DiffRow {
  kind: 'added' | 'removed' | 'changed'
  /** 展示用的标识，比如实体类型名、或 "主语 -关系-> 宾语"。 */
  label: string
  /** changed 时说明改了什么；added/removed 时为空。 */
  detail?: string
}

export interface OntologyDiff {
  termTypes: DiffRow[]
  relationTypes: DiffRow[]
  constraints: DiffRow[]
  total: number
}

/** 按一个稳定的 key 把两边对齐，得出增/删/改。 */
function diffByKey<T>(
  draft: T[],
  confirmed: T[],
  keyOf: (item: T) => string,
  labelOf: (item: T) => string,
  describeChange?: (before: T, after: T) => string | null,
): DiffRow[] {
  const draftByKey = new Map(draft.map((item) => [keyOf(item), item]))
  const confirmedByKey = new Map(confirmed.map((item) => [keyOf(item), item]))
  const rows: DiffRow[] = []

  for (const [key, item] of draftByKey) {
    const before = confirmedByKey.get(key)
    if (!before) {
      rows.push({ kind: 'added', label: labelOf(item) })
      continue
    }
    const detail = describeChange?.(before, item)
    if (detail) rows.push({ kind: 'changed', label: labelOf(item), detail })
  }
  for (const [key, item] of confirmedByKey) {
    if (!draftByKey.has(key)) rows.push({ kind: 'removed', label: labelOf(item) })
  }
  return rows
}

export interface TermTypeLike {
  value: string
  extra_fields: { name: string; value_type: string }[]
}

export interface RelationTypeLike {
  relation_type: string
  example_phrase?: string
  description?: string
  allow_chain_query?: boolean
}

export interface ConstraintLike {
  subject_term_type: string
  relation_type: string
  object_term_type: string
}

/** 字段列表按名字排序后比对——顺序变化不是语义变化，不该报成"改了"。 */
function describeFieldChange(before: TermTypeLike, after: TermTypeLike): string | null {
  const render = (t: TermTypeLike) =>
    t.extra_fields
      .map((f) => `${f.name}:${f.value_type}`)
      .sort()
      .join(', ')
  const a = render(before)
  const b = render(after)
  return a === b ? null : `属性字段 ${a || '（无）'} → ${b || '（无）'}`
}

function describeRelationChange(
  before: RelationTypeLike,
  after: RelationTypeLike,
): string | null {
  const parts: string[] = []
  if ((before.example_phrase ?? '') !== (after.example_phrase ?? '')) {
    parts.push(`例句 "${before.example_phrase ?? ''}" → "${after.example_phrase ?? ''}"`)
  }
  if ((before.description ?? '') !== (after.description ?? '')) {
    parts.push(`说明 "${before.description ?? ''}" → "${after.description ?? ''}"`)
  }
  if ((before.allow_chain_query ?? true) !== (after.allow_chain_query ?? true)) {
    parts.push(`可链式查询 ${before.allow_chain_query} → ${after.allow_chain_query}`)
  }
  return parts.length > 0 ? parts.join('；') : null
}

export function buildOntologyDiff(
  draft: {
    termTypes: TermTypeLike[]
    relationTypes: RelationTypeLike[]
    constraints: ConstraintLike[]
  },
  confirmed: {
    termTypes: TermTypeLike[]
    relationTypes: RelationTypeLike[]
    constraints: ConstraintLike[]
  },
): OntologyDiff {
  const termTypes = diffByKey(
    draft.termTypes, confirmed.termTypes,
    (t) => t.value, (t) => t.value, describeFieldChange,
  )
  const relationTypes = diffByKey(
    draft.relationTypes, confirmed.relationTypes,
    (r) => r.relation_type, (r) => r.relation_type, describeRelationChange,
  )
  const constraints = diffByKey(
    draft.constraints, confirmed.constraints,
    (c) => `${c.subject_term_type}|${c.relation_type}|${c.object_term_type}`,
    (c) => `${c.subject_term_type} -${c.relation_type}-> ${c.object_term_type}`,
  )
  return {
    termTypes,
    relationTypes,
    constraints,
    total: termTypes.length + relationTypes.length + constraints.length,
  }
}
