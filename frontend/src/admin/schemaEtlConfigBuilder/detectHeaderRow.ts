/** 只看前这么多行。表头不可能在第 20 行以后，多扫只是浪费。 */
export const DETECT_SCAN_ROWS = 20

/** 打分时往下看几行。 */
const LOOKAHEAD_ROWS = 5

/**
 * 猜表头在第几行（1-based）。
 *
 * **这只是建议，永远不自动生效。** 界面要显示"猜的是第 N 行"并让用户改——
 * 推断提议、人确认，跟 Foundry 的 schema 推断对话框是同一个姿态。自动生效
 * 的推断一旦猜错，用户看到的是一份莫名其妙的数据，而不是一个可以改的选项。
 *
 * 打分 = 该行非空单元格数 × 其后几行的平均非空率。
 *
 * 光看非空单元格数不够：顶上的说明块（"注意：本表仅供内部使用……"）可能比
 * 真表头还满。乘上"其后几行的非空率"就能把它排掉——说明块下面通常是空行，
 * 真表头下面是密密麻麻的数据。
 */
export function detectHeaderRow(rows: string[][]): number {
  if (rows.length < 2) return 1

  let bestRow = 1
  let bestScore = -1
  const limit = Math.min(rows.length, DETECT_SCAN_ROWS)

  for (let i = 0; i < limit; i++) {
    const width = Math.max(rows[i].length, 1)
    const nonEmpty = rows[i].filter((cell) => cell.trim() !== '').length
    const lookahead = rows.slice(i + 1, i + 1 + LOOKAHEAD_ROWS)
    if (lookahead.length === 0) continue
    const fillRate =
      lookahead.reduce(
        (sum, row) => sum + row.filter((cell) => cell.trim() !== '').length / width,
        0,
      ) / lookahead.length
    const score = nonEmpty * fillRate
    // 严格大于：并列时保留更靠上的那一行。表头之后才是数据，而数据行长得
    // 跟表头一样满，分数会打平——这时候靠上的那个才是表头。
    if (score > bestScore) {
      bestScore = score
      bestRow = i + 1
    }
  }
  return bestRow
}
