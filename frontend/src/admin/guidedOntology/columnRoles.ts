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
  // 与 columnStats.ts 的 NUMERIC_IDENTIFIER_THRESHOLD(=50) 的关系：那道门在
  // 扫描阶段把 distinct > 50 的无小数数值列的 inferredType 改判成 'string'。
  // 它**不**收窄下面这条整数判定——所以这里判"是不是整数列"一律用
  // stats.isWholeNumber（扫描期的原始观察），不用 inferredType。
  //
  // 曾经写在这里的一条因果是错的，如实记下以免再犯：旧注释声称"上面那道门
  // 要求 distinct ≤ 50，所以整数被误判成标识的窗口只在 20 到 55 行之间"。
  // 实测推翻——被改判成 'string' 的列此前根本不受 INTEGER_IDENTIFIER_MIN_ROWS
  // 约束（isInteger 当时只看 inferredType），于是 10000 行 / 9800 个不同值的
  // "金额分"一样会被判成 identifier，窗口在 50 个不同值以上是无上界的。
  // 现在 isWholeNumber 让行数下限对这些列也生效，但那只挡住了行数太少的
  // 一半：一列高基数整数度量在分布上和一列真订单号无法区分，任何阈值都只是
  // 在两种猜法之间选一种。真正的兜底不在这里，而在审阅视图——标识列现在会
  // 连同 reason 一起展示，并且能被改判成属性（ProposalReview 的「被当成标识
  // 的列」一节）。判错时用户看得见、推得翻。
  if (column.inferredType === 'number') {
    return {
      role: 'measure',
      reason: `带小数的数值列，通常是度量（样例 ${column.samples[0] ?? ''}）`,
    }
  }

  // 用扫描期的原始观察，不用 inferredType：inferType 已经把 distinct > 50 的
  // 无小数数值列改判成 'string' 了，只看 inferredType 会让下面这两条（行数
  // 下限、整数专属文案）对数量/单价/以分为单位的金额这些真正需要它们的列
  // 一律不生效。
  const isInteger = column.isWholeNumber
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
    // 页就有那个入口）。这段文案现在会渲染进审阅视图的「没有用到的列」
    // 小节（ProposalReview.tsx 按列显示 reason），所以这里的因果必须
    // 站得住——不能只对模型层的单测成立。
    //
    // 两个不同的落空理由，措辞不能共用：
    // - 行数不够（ratio 已经 >= IDENTIFIER_MIN_RATIO，只是 nonEmptyCount
    //   不到 INTEGER_IDENTIFIER_MIN_ROWS）：这一列其实"几乎每行一个"，
    //   说它"没高到每行一个"是假的——真实原因是样本太小。
    // - 比例本身就不上不下（介于 DIMENSION_MAX_RATIO 和
    //   IDENTIFIER_MIN_RATIO 之间）：这才是"重复度不够当分类，也没高到
    //   每行一个"这句话本来描述的情况。
    const looksLikeIdentifierButTooFewRows = !enoughRowsForIdentifier && ratio >= IDENTIFIER_MIN_RATIO
    if (looksLikeIdentifierButTooFewRows) {
      return {
        role: 'freetext',
        reason:
          `${column.nonEmptyCount} 个非空值里有 ${column.distinctCount} 个不同值，几乎每行一个，` +
          `但只有 ${column.nonEmptyCount} 行——样本太小，不能当标识（整数列至少要 ` +
          `${INTEGER_IDENTIFIER_MIN_ROWS} 行）。整数列不会被自动当成度量（只有带小数的` +
          '数值列才会）——如果它是金额或计数，建完草稿后去「本体结构」页把它手工加成属性。',
      }
    }
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
