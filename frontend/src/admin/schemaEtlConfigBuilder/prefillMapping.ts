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
      return {
        source: 'stored',
        entities,
        relations,
        unusedColumns: columns.filter((c) => !used.has(c)),
        unmatchedTermTypes: [],
        storedMissingColumns: null,
      }
    }
    const suggestion = suggestMapping({ columns, termTypes, combinations, fileId })
    return { source: 'suggested', ...suggestion, storedMissingColumns: missing }
  }

  const suggestion = suggestMapping({ columns, termTypes, combinations, fileId })
  return { source: 'suggested', ...suggestion, storedMissingColumns: null }
}
