import type { ColumnRole, ColumnStats, RoledColumn } from './types'

/**
 * 不同值占非空行数的比例低于这个值，才算维度候选。
 *
 * 高于它的字符串列是自由文本（备注、描述）：把 600 个不同值的列建成实体
 * 类型，会造出 600 个彼此无关的节点。
 */
export const DIMENSION_MAX_RATIO = 0.2

/** 高于这个比例就认为"几乎每行一个"，是本行的标识。 */
const IDENTIFIER_MIN_RATIO = 0.9

export function assignRoles(stats: ColumnStats[]): RoledColumn[] {
  return stats.map((column) => {
    const { role, reason } = classify(column)
    return { stats: column, role, reason }
  })
}

function classify(column: ColumnStats): { role: ColumnRole; reason: string } {
  if (column.nonEmptyCount === 0) {
    // 建成实体类型的话，会造出一个没有任何实例的类型。
    return { role: 'freetext', reason: '这一列全是空的（0 个非空值）' }
  }
  if (column.inferredType === 'date') {
    return { role: 'date', reason: `识别为日期，样例 ${column.samples[0] ?? ''}` }
  }
  if (column.inferredType === 'number' || column.inferredType === 'integer') {
    return {
      role: 'measure',
      reason: `数值列，通常是度量（样例 ${column.samples[0] ?? ''}）`,
    }
  }

  const ratio = column.distinctCount / column.nonEmptyCount
  if (column.distinctCapped || ratio >= IDENTIFIER_MIN_RATIO) {
    const count = column.distinctCapped ? `超过 ${column.distinctCount}` : `${column.distinctCount}`
    return {
      role: 'identifier',
      reason: `${column.nonEmptyCount} 个非空值里有 ${count} 个不同值，几乎每行一个`,
    }
  }
  if (ratio <= DIMENSION_MAX_RATIO) {
    return {
      role: 'dimension',
      reason: `${column.nonEmptyCount} 个非空值里只有 ${column.distinctCount} 个不同值，重复度高`,
    }
  }
  return {
    role: 'freetext',
    reason: `${column.nonEmptyCount} 个非空值里有 ${column.distinctCount} 个不同值，重复度不足以当分类`,
  }
}
