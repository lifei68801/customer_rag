import type { RoledColumn } from '../guidedOntology/types'
import { normalizeAlias } from './aliases'
import type {
  WorkspaceConstraint,
  WorkspaceRelationType,
  WorkspaceState,
  WorkspaceTermType,
} from './types'

export interface ScannedTable {
  file: string
  roled: RoledColumn[]
}

export interface TableMatch {
  termValue: string
  keyColumns: string[]
  fieldColumns: Record<string, string>
  /** `alias:<原别名>`。界面要显示依据——用户看到"按别名 jan 对上"才判断得了对不对。 */
  matchedBy: string
}

export interface TableAlignment {
  file: string
  matches: TableMatch[]
  unmatchedColumns: string[]
}

/**
 * 哪些角色能提升为实体类型。度量/自由文本/日期提上来会给每个金额建一个
 * 节点——export 给 DataPanel 用，那边按这个集合决定要不要显示"提升为实体
 * 类型"按钮。
 */
export const PROMOTABLE_ROLES = new Set(['identifier', 'dimension'])

/**
 * 把一张表的列对齐到骨架。
 *
 * 命中规则是确定性的：别名归一化后与列名精确相等才算命中，命不中就命不中。
 * 不做前缀/包含/编辑距离——猜错一列会把错误数据写进图谱，而这一步失败的代价
 * 只是用户手动指一下列（spec 行为规格 §3）。
 */
export function alignTable(table: ScannedTable, termTypes: WorkspaceTermType[]): TableAlignment {
  const columns = table.roled.map((c) => c.stats.name)
  const used = new Set<string>()
  const matches: TableMatch[] = []

  // 两遍扫描，不是一遍：字段匹配要排除**所有**实体的键列，不能只排除自己
  // 的。一遍扫描时后面的实体做字段匹配那一刻，还不知道排在它后面的实体
  // 会认哪一列做键列，于是实体 A 的字段别名可能抢先认领了实体 B 将来的
  // 键列——谁抢到谁没抢到，取决于 termTypes 的遍历顺序，是纯粹的巧合。
  // 第一遍只认键列，把全部键列收集齐；第二遍做字段匹配时才有完整的排除
  // 集合可用。
  const keyMatchOf = new Map<string, { column: string; matchedBy: string }>()
  const allKeyColumns = new Set<string>()
  for (const term of termTypes) {
    if (term.review === 'rejected') continue
    let keyColumn: string | null = null
    let matchedBy = ''
    for (const alias of term.key_aliases) {
      const hit = columns.find((name) => normalizeAlias(name) === normalizeAlias(alias))
      if (hit !== undefined) {
        keyColumn = hit
        matchedBy = `alias:${alias}`
        break
      }
    }
    if (keyColumn === null) continue
    keyMatchOf.set(term.value, { column: keyColumn, matchedBy })
    allKeyColumns.add(keyColumn)
  }

  for (const term of termTypes) {
    const keyMatch = keyMatchOf.get(term.value)
    if (!keyMatch) continue
    const { column: keyColumn, matchedBy } = keyMatch
    const fieldColumns: Record<string, string> = {}
    for (const [fieldName, aliases] of Object.entries(term.field_aliases)) {
      for (const alias of aliases) {
        const hit = columns.find(
          (name) => !allKeyColumns.has(name) && normalizeAlias(name) === normalizeAlias(alias),
        )
        if (hit !== undefined) {
          fieldColumns[fieldName] = hit
          break
        }
      }
    }
    used.add(keyColumn)
    for (const column of Object.values(fieldColumns)) used.add(column)
    matches.push({ termValue: term.value, keyColumns: [keyColumn], fieldColumns, matchedBy })
  }

  return {
    file: table.file,
    matches,
    // 未被任何实体用作键/字段的列全数返回，不按角色过滤：度量/文本/日期列
    // 命不中骨架时也要能在数据面板里看见并手动指给某个实体当字段（能不能
    // 提升为实体类型是 PROMOTABLE_ROLES 管的事，跟"要不要展示"是两回事）。
    unmatchedColumns: columns.filter((name) => !used.has(name)),
  }
}

interface RoleSighting {
  file: string
  role: string
  /** 这张表里这一列所属的实体（也就是这张表的"主语候选"）。 */
  ownerTermValue: string
}

/**
 * 表之间的关系提议（spec 决策 13）。
 *
 * 判据只有一条：同一个实体类型的键列，在 A 表里是 identifier（每行一个，
 * 说明这张表讲的就是它）、在 B 表里是 dimension（重复出现，说明 B 表的行
 * 指向它）。那就提一条 B 的实体 → A 的实体的关系。
 *
 * **不做值级 join 分析**：那要把两张表的列值都读进来比对，代价是又一遍全表
 * 扫描，而结论并不更可靠——两列值域重合不代表有业务关系。
 */
export function proposeCrossTableRelations(
  alignments: TableAlignment[],
  tables: ScannedTable[],
  state: WorkspaceState,
): WorkspaceRelationType[] {
  return proposeCrossTable(alignments, tables, state).relations
}

export function proposeCrossTableConstraints(
  alignments: TableAlignment[],
  tables: ScannedTable[],
  state: WorkspaceState,
): WorkspaceConstraint[] {
  return proposeCrossTable(alignments, tables, state).constraints
}

function proposeCrossTable(
  alignments: TableAlignment[],
  tables: ScannedTable[],
  state: WorkspaceState,
): { relations: WorkspaceRelationType[]; constraints: WorkspaceConstraint[] } {
  const roleByFile = new Map(
    tables.map((t) => [t.file, new Map(t.roled.map((c) => [c.stats.name, String(c.role)]))]),
  )
  // 每张表的"主语"：这张表里角色是 identifier 的那个命中实体。
  const ownerOf = new Map<string, string>()
  for (const alignment of alignments) {
    const owner = alignment.matches.find(
      (m) => roleByFile.get(alignment.file)?.get(m.keyColumns[0]) === 'identifier',
    )
    if (owner) ownerOf.set(alignment.file, owner.termValue)
  }

  const sightings = new Map<string, RoleSighting[]>()
  for (const alignment of alignments) {
    for (const match of alignment.matches) {
      const role = roleByFile.get(alignment.file)?.get(match.keyColumns[0]) ?? ''
      const owner = ownerOf.get(alignment.file)
      if (!owner) continue
      const list = sightings.get(match.termValue) ?? []
      list.push({ file: alignment.file, role, ownerTermValue: owner })
      sightings.set(match.termValue, list)
    }
  }

  const relations: WorkspaceRelationType[] = []
  const constraints: WorkspaceConstraint[] = []
  const seen = new Set<string>()
  for (const [termValue, list] of sightings) {
    const identifierSide = list.find((s) => s.role === 'identifier')
    if (!identifierSide) continue
    for (const dimensionSide of list) {
      if (dimensionSide.role !== 'dimension') continue
      const subject = dimensionSide.ownerTermValue
      const object = identifierSide.ownerTermValue
      if (subject === object) continue
      const known = state.constraints.find((c) => c.subject === subject && c.object === object)
      const relationType = known?.relation ?? 'RELATES_TO'
      const key = `${subject}|${relationType}|${object}`
      if (seen.has(key)) continue
      seen.add(key)
      relations.push({
        relation_type: relationType,
        example_phrase: `${subject} 关联 ${object}`,
        description: `由 ${dimensionSide.file} 与 ${identifierSide.file} 里同名的 ${termValue} 列推出`,
        provenance: 'data',
        review: 'pending',
        clues: [],
        data_match: null,
      })
      constraints.push({
        subject,
        relation: relationType,
        object,
        provenance: 'data',
        // 约束不单独审阅：进不进草稿由主语/宾语/关系三个元素的审阅决定
        // （见 projectToDraft）。pending 只是占位，rejected 留给 v2 单独拒绝
        // 一条约束用。
        review: 'pending',
      })
    }
  }
  return { relations, constraints }
}

/**
 * 把对齐结果合并进工作区状态。**纯函数**：返回新对象，不改入参。
 *
 * 合并而不是覆盖，是因为用户可能已经改过名、做过审阅决定（spec 里 discover
 * 不写库的理由就是这个）。这里只动 data_match 和 unmatched_columns 两处，
 * review / display_name / extra_fields 一律照抄。
 */
export function mergeAlignments(
  state: WorkspaceState,
  alignments: TableAlignment[],
  tables: ScannedTable[],
): WorkspaceState {
  const matchOf = new Map<string, { file: string; match: TableMatch }>()
  for (const alignment of alignments) {
    for (const match of alignment.matches) {
      // 一个实体在多张表里命中时保留第一张：ETL 映射里一个实体一条 entities
      // 条目。改选要去表格导入页手动配——v1 没有改选界面。
      if (!matchOf.has(match.termValue)) matchOf.set(match.termValue, { file: alignment.file, match })
    }
  }

  const termTypes = state.term_types.map((term) => {
    const found = matchOf.get(term.value)
    if (!found) return term
    return {
      ...term,
      data_match: {
        source_file: found.file,
        key_columns: [...found.match.keyColumns],
        field_columns: { ...found.match.fieldColumns },
        matched_by: found.match.matchedBy,
      },
    }
  })

  const unmatched = { ...state.unmatched_columns }
  for (const alignment of alignments) {
    // 覆盖而不是并集：重新扫描同一张表就是要拿这一次的结论，并集会把用户
    // 上一次已经提升掉的列又列出来。
    unmatched[alignment.file] = [...alignment.unmatchedColumns]
  }

  const { relations, constraints } = proposeCrossTable(alignments, tables, state)
  const existingRelations = new Set(state.relation_types.map((r) => r.relation_type))
  const existingConstraints = new Set(
    state.constraints.map((c) => `${c.subject}|${c.relation}|${c.object}`),
  )

  const sources = [...state.sources]
  for (const table of tables) {
    if (!sources.some((s) => s.file === table.file)) sources.push({ file: table.file })
  }

  return {
    ...state,
    term_types: termTypes,
    relation_types: [
      ...state.relation_types,
      ...relations.filter((r) => !existingRelations.has(r.relation_type)),
    ],
    constraints: [
      ...state.constraints,
      ...constraints.filter(
        (c) => !existingConstraints.has(`${c.subject}|${c.relation}|${c.object}`),
      ),
    ],
    sources,
    unmatched_columns: unmatched,
  }
}
