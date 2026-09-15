import type { EtlMappingSummary } from '../etlMappingApi'
import { suggestMapping } from './suggestMapping'
import { summaryToBuilder } from './summaryToBuilder'
import type { BuilderEntity, BuilderRelation, ConfirmedCombination, ConfirmedTermType } from './types'

/**
 * 用户刚选完文件，第二步要显示什么——这一个函数把"沿用上次"和"现推一份"
 * 之间的选择集中在一处。
 *
 * 分散着写的话，两条路径会各自长出自己的判断，而它们的差别（沿用的那份
 * 提交时不带 config、推出来的那份必须带）只有在提交那一刻才显现，界面上
 * 看不出来。
 */
export interface Prefill {
  /** stored=沿用存着的那份；suggested=按本体现推的一份。 */
  source: 'stored' | 'suggested'
  entities: BuilderEntity[]
  relations: BuilderRelation[]
  /** 这张表里没被用到的列。 */
  unusedColumns: string[]
  /** 本体里有、这张表里没有对应列的实体类型。沿用存着的映射时为空。 */
  unmatchedTermTypes: string[]
  /**
   * 存着映射、但没沿用时，列出它引用了而这张表没有的列。
   *
   * 必须说出来：用户看到的是"这不是我上次配的那份"，不给原因的话，最可能
   * 的猜测是"系统把我的配置弄丢了"。真实原因通常是他换了一张列名不同的表。
   */
  storedMissingColumns: string[] | null
  /**
   * 存着的映射里，关系的主体/客体已经不在已确认本体里，按本体自动改过的那些。
   *
   * 真实事故：用户把 `Product —HAS_COMPANY→ Company` 改成
   * `Order ID —HAS_COMPANY→ Company` 并确认了本体，但存着的映射仍指着
   * Product。ETL 按本体校验，这些边被全部跳过，而跑批报告是"成功"——
   * 图里一条公司边都没有，问"某公司有多少订单"答"没有订单"。他连着撞了
   * 两次，每次都以为是别的地方出了问题。
   */
  repairedRelations: { relationType: string; fromSubject: string; toSubject: string }[]
  /**
   * 对不上、又没法自动改的关系（同名关系在本体里有多种组合，或者压根没有了）。
   * 直接丢掉——留着也只会被 ETL 静默跳过，而界面上看着像配好了。
   */
  droppedRelations: { subject: string; relationType: string; object: string }[]
}

/**
 * 拿已确认本体校一遍存着的关系映射。
 *
 * 能自动改的只有一种情况：同一个关系类型在本体里**只剩一种**组合，且客体
 * 类型没变。那时"主体换成本体里的那个"是唯一可能的意图，不是猜。多于一种
 * 组合时不猜——猜错会把边写到错误的实体上，比不写更难发现。
 */
function reconcileRelations(
  relations: BuilderRelation[],
  combinations: ConfirmedCombination[],
): {
  relations: BuilderRelation[]
  repaired: Prefill['repairedRelations']
  dropped: Prefill['droppedRelations']
} {
  // 一条允许组合都没有 = **不知道**本体长什么样，不是"什么都不允许"。
  //
  // 页面是异步拉本体的：用户在那个请求回来之前就选了文件时，这里拿到的是空
  // 列表；请求失败时也是空。把空当成"都不允许"会把映射里的关系全部清掉，
  // 而用户什么都没做错（第一版就是这样，三条既有用例因此变红）。
  if (combinations.length === 0) {
    return { relations, repaired: [], dropped: [] }
  }
  const allowed = new Set(
    combinations.map((c) => `${c.subject_term_type}|${c.relation_type}|${c.object_term_type}`),
  )
  const kept: BuilderRelation[] = []
  const repaired: Prefill['repairedRelations'] = []
  const dropped: Prefill['droppedRelations'] = []

  for (const relation of relations) {
    const key = `${relation.subjectTermType}|${relation.relationType}|${relation.objectTermType}`
    if (allowed.has(key)) {
      kept.push(relation)
      continue
    }
    const sameRelation = combinations.filter((c) => c.relation_type === relation.relationType)
    if (sameRelation.length === 1) {
      const target = sameRelation[0]
      kept.push({
        ...relation,
        subjectTermType: target.subject_term_type,
        objectTermType: target.object_term_type,
      })
      repaired.push({
        relationType: relation.relationType,
        fromSubject: relation.subjectTermType,
        toSubject: target.subject_term_type,
      })
      continue
    }
    dropped.push({
      subject: relation.subjectTermType,
      relationType: relation.relationType,
      object: relation.objectTermType,
    })
  }
  return { relations: kept, repaired, dropped }
}

/** 这份映射引用到的全部列名。少算一个，就会把一份跑不通的映射当成能沿用。 */
function referencedColumns(summary: EtlMappingSummary): string[] {
  const columns: string[] = []
  for (const entity of summary.entities) {
    columns.push(...entity.name_columns)
    for (const part of entity.key_parts) {
      if (part.kind === 'column') columns.push(part.column)
      else columns.push(...part.scope_columns, part.raw_value_column)
    }
    columns.push(...Object.values(entity.attributes))
  }
  return columns
}

export function prefillMapping(options: {
  columns: string[]
  summary: EtlMappingSummary | null | undefined
  termTypes: ConfirmedTermType[]
  combinations: ConfirmedCombination[]
  fileId: string
}): Prefill {
  const { columns, summary, termTypes, combinations, fileId } = options

  if (summary) {
    const referenced = referencedColumns(summary)
    const missing = [...new Set(referenced.filter((c) => !columns.includes(c)))]
    if (missing.length === 0) {
      const { entities, relations } = summaryToBuilder(summary, fileId)
      const used = new Set(referenced)
      // 存着的映射可能是在本体改动之前配的。列对得上不代表关系还成立——
      // 本体改了组合之后，旧映射里的关系会被 ETL 静默跳过，而跑批报告是
      // "成功"。所以这里再按本体校一遍。
      const reconciled = reconcileRelations(relations, combinations)
      return {
        source: 'stored',
        entities,
        relations: reconciled.relations,
        unusedColumns: columns.filter((c) => !used.has(c)),
        unmatchedTermTypes: [],
        storedMissingColumns: null,
        repairedRelations: reconciled.repaired,
        droppedRelations: reconciled.dropped,
      }
    }
    const suggestion = suggestMapping({ columns, termTypes, combinations, fileId })
    return {
      source: 'suggested',
      ...suggestion,
      storedMissingColumns: missing,
      // 现推的映射本来就是按当前本体生成的，不存在对不上的关系。
      repairedRelations: [],
      droppedRelations: [],
    }
  }

  const suggestion = suggestMapping({ columns, termTypes, combinations, fileId })
  return {
    source: 'suggested',
    ...suggestion,
    storedMissingColumns: null,
    repairedRelations: [],
    droppedRelations: [],
  }
}
