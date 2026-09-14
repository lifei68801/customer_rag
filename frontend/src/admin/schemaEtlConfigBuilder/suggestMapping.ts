import type {
  BuilderEntity,
  BuilderRelation,
  ConfirmedCombination,
  ConfirmedTermType,
} from './types'

/**
 * 拿"已确认的本体"去对"刚上传的这张表的列"，推出一份可以直接编辑的映射。
 *
 * ## 为什么建议来自本体，而不是来自列的统计分布
 *
 * 引导建模页那套（assignRoles 按基数判 identifier/dimension/measure）解决的是
 * 另一个问题：**本体还不存在**，要从零推一份出来。到了表格导入页，本体已经
 * 确认过了，实体类型和它们的属性字段都是定好的——这时要回答的不是"哪列该
 * 建成实体"，而是"这张表的哪一列，对应本体里已有的哪个东西"。拿分布去猜
 * 只会在本体已经给出答案的地方引入第二个答案。
 *
 * ## 对不上怎么办
 *
 * 对不上的列进 unusedColumns，对不上的实体类型进 unmatchedTermTypes，两边
 * 都摆给用户看。**不在这里新建实体类型**——那是本体建模的职责，在表格导入
 * 页偷偷长出一个新类型，会让"已确认本体"这件事失去意义。
 *
 * ## 这个函数不做冲突预检
 *
 * 它照本体说的配，本体说邮编挂在客户名下，它就这么配。那份配置对不对是
 * 另一回事，由扫全表的单值检测（columnStats.scanPairs）回答——两件事混在
 * 一起的话，"建议"就会变成一个既照本体又不照本体的东西，用户无从预期。
 */
export interface MappingSuggestion {
  entities: BuilderEntity[]
  relations: BuilderRelation[]
  /** 这张表里没被任何实体用到的列（既不是键/展示名，也不是谁的属性）。 */
  unusedColumns: string[]
  /** 本体里有、但这张表里找不到对应列的实体类型。 */
  unmatchedTermTypes: string[]
}

/**
 * 比对用的归一化形式：忽略大小写、空格、下划线、连字符和点。
 *
 * 需要它是因为同一个名字在三个地方各有各的写法：本体里的字段内部名是
 * `Customer_Zip_Code`（sanitizeFieldName 把空格换成了下划线），它的显示名
 * 是 `Customer Zip Code`，而表头可能写成 `customer zip code`。只按精确匹配
 * 的话，一张列名大小写不同的表会一条都对不上，用户看到的是一份空建议。
 */
function normalize(value: string): string {
  return value.toLowerCase().replace(/[\s_\-.]+/g, '')
}

/** 按"精确 → 归一化"两档找列。归一化撞上多列时不猜，当作没找到。 */
function findColumn(columns: string[], ...candidates: string[]): string | null {
  for (const candidate of candidates) {
    if (!candidate) continue
    const exact = columns.find((c) => c === candidate)
    if (exact !== undefined) return exact
  }
  for (const candidate of candidates) {
    if (!candidate) continue
    const target = normalize(candidate)
    const hits = columns.filter((c) => normalize(c) === target)
    // 撞上多列时只能靠猜，而猜错的代价是把数据写到另一列上——宁可让这一条
    // 留空，用户在下拉里自己选。
    if (hits.length === 1) return hits[0]
  }
  return null
}

export function suggestMapping(options: {
  columns: string[]
  termTypes: ConfirmedTermType[]
  combinations: ConfirmedCombination[]
  fileId: string
}): MappingSuggestion {
  const { columns, termTypes, combinations, fileId } = options
  const entities: BuilderEntity[] = []
  const unmatchedTermTypes: string[] = []
  const usedColumns = new Set<string>()

  for (const termType of termTypes) {
    const column = findColumn(columns, termType.value)
    if (column === null) {
      unmatchedTermTypes.push(termType.value)
      continue
    }
    usedColumns.add(column)
    const fieldMappings: Record<string, string> = {}
    for (const field of termType.extra_fields) {
      // 显示名优先：它是建模时那一列的原始列名，最可能跟表头一字不差。
      // 内部名是被 sanitizeFieldName 改写过的产物，只能当退路。
      const source = findColumn(columns, field.label ?? '', field.name)
      if (source === null) continue
      fieldMappings[field.name] = source
      usedColumns.add(source)
    }
    entities.push({
      id: `suggested-${termType.value}`,
      termType: termType.value,
      fileId,
      standardNameColumn: column,
      nodeKeyParts: [{ kind: 'column', column }],
      fieldMappings,
    })
  }

  const matched = new Set(entities.map((e) => e.termType))
  const relations: BuilderRelation[] = combinations
    // 两端都得在这张表里找得到，否则这条边没有数据来源。
    .filter((c) => matched.has(c.subject_term_type) && matched.has(c.object_term_type))
    .map((c, index) => ({
      id: `suggested-rel-${index}`,
      fileId,
      subjectTermType: c.subject_term_type,
      relationType: c.relation_type,
      objectTermType: c.object_term_type,
    }))

  return {
    entities,
    relations,
    unusedColumns: columns.filter((c) => !usedColumns.has(c)),
    unmatchedTermTypes,
  }
}
