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

/**
 * 整数列要判成标识，非空行数至少这么多。
 *
 * 取 20 的依据：ratio 是一个比例估计，行数越少它越不稳——3 行整数金额
 * 恰好互不相同（ratio 1.0）在真实数据里再普通不过，那不是"每行一个值"
 * 的证据，只是样本太小。20 行以下几乎任何整数度量都可能碰巧全不重复；
 * 到 20 行，一列整数还能保持互不相同，才开始像个编号。
 *
 * 只卡整数，不卡字符串：字符串标识（ORD1001 这种）本来就长得像编号，
 * 而且它没有"其实是度量"的另一种可能，卡它只会误伤小表。
 *
 * 卡的是**静默**那条出口：判成 identifier 会让这一列直接成为本体的中心，
 * 而审阅视图里根本不展示标识列，用户看不见也推不翻。不够行数的整数列会
 * 落进 freetext，出现在「没有用到的列」里——可见，可纠正。
 */
const INTEGER_IDENTIFIER_MIN_ROWS = 20

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
  // 只有带小数的数值列（number）短路成度量。真实的连续型度量——金额、
  // 比率、百分比——几乎都带小数，这条短路的误判风险低。
  //
  // integer 不短路，走下面和字符串列同一条比例判定：整数既可能是编号
  // （门店编号、状态码、45 行的小表里的订单号），也可能是计数，光看类型
  // 分不出来，必须看分布。
  //
  // 这样分的决定性理由不是"准确率更高"，是**错误的可见性**：判成
  // measure 的列在审阅视图里根本不展示（ProposalReview 只列 dimension），
  // 用户看不见也无法纠正，误判会静默写进本体草稿；而判成 dimension 的列
  // 会出现在审阅视图里，让用户在「建成实体 / 做成属性」之间二选一。宁可
  // 判错成用户能看见并推翻的东西，也不要判错成他永远看不见的。
  //
  // 与 columnStats.ts 的 NUMERIC_IDENTIFIER_THRESHOLD(=50) 协同而不重复：
  // 那道门在扫描阶段就把 distinct > 50 的整数列重分类成 'string'，所以能
  // 走到这里的 integer 列 distinct 一定 ≤ 50。
  //
  // 残余风险（如实记下，不是已解决的问题）：高基数整数度量仍可能因
  // ratio >= 0.9 被判成 identifier 而静默成为根。窗口被两头夹住——上面那道
  // 门要求 distinct ≤ 50（于是 ratio >= 0.9 意味着 nonEmptyCount 大约 ≤ 55），
  // 下面的 INTEGER_IDENTIFIER_MIN_ROWS 要求 nonEmptyCount >= 20——所以只在
  // 20 到 55 行之间、且整数列几乎全不重复时才可能发生。低基数的整数度量
  // （units_sold 取值 1..20）会被判成 dimension——那是可见可纠正的。
  if (column.inferredType === 'number') {
    return {
      role: 'measure',
      reason: `带小数的数值列，通常是度量（样例 ${column.samples[0] ?? ''}）`,
    }
  }

  const isInteger = column.inferredType === 'integer'
  const ratio = column.distinctCount / column.nonEmptyCount
  const enoughRowsForIdentifier =
    !isInteger || column.nonEmptyCount >= INTEGER_IDENTIFIER_MIN_ROWS
  if (enoughRowsForIdentifier && (column.distinctCapped || ratio >= IDENTIFIER_MIN_RATIO)) {
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
  if (isInteger) {
    // 整数列专用文案。通用那句「重复度不足以当分类」对一列整数编号解释力
    // 很弱，对一列整元单价更是指错了方向——那一列的问题不是"不够格当
    // 分类"，是系统没认出它是金额，于是整列没进本体。给一个真能做到的
    // 动作：建完之后去「本体结构」页手工加成属性（GuidedOntologyPage 完成
    // 页就有那个入口）。
    return {
      role: 'freetext',
      reason:
        `${column.nonEmptyCount} 个非空值里有 ${column.distinctCount} 个不同值：` +
        '重复度不够当分类，也没高到每行一个。整数列不会被自动当成度量（只有带小数的' +
        '数值列才会）——如果它是金额或计数，建完草稿后去「本体结构」页把它手工加成属性。',
    }
  }
  return {
    role: 'freetext',
    reason: `${column.nonEmptyCount} 个非空值里有 ${column.distinctCount} 个不同值，重复度不足以当分类`,
  }
}
