/** 只看前这么多行。表头不可能在第 20 行以后，多扫只是浪费。 */
export const DETECT_SCAN_ROWS = 20

/**
 * 判断"接下来是否稳定"要往下看几行。至少要 2 行才有意义——1 行的窗口
 * 永远只有一个值，落差恒为 0，会把每一行都误判成"稳定"（用真实 MUJI 数据
 * 验证过：改成 1 后，第 1 行就会因为落差 0 被立即选中）。
 */
const LOOKAHEAD_ROWS = 5

/**
 * 接下来几行的非空率落差在多少以内算"稳定"。用真实 MUJI 文件（113 列）核算
 * 过：表头堆里第 2~5 行（几乎空 / 英文名 / 日文名 / 说明行）往下看 5 行，落差
 * 都是 0.3186（108/113 − 72/113，因为窗口里还混着别的表头层）；真表头（第 6
 * 行）往下看 5 行，落差是 0（连续 5 行都是同密度的数据）。0.2 卡在两者中间，
 * 留了安全余量。
 */
const STABILITY_THRESHOLD = 0.2

/**
 * 猜表头在第几行（1-based）。
 *
 * **这只是建议，永远不自动生效。** 界面要显示"猜的是第 N 行"并让用户改——
 * 推断提议、人确认，跟 Foundry 的 schema 推断对话框是同一个姿态。自动生效
 * 的推断一旦猜错，用户看到的是一份莫名其妙的数据，而不是一个可以改的选项。
 *
 * 从上往下扫描，返回第一个"自己非空、且接下来几行非空率很稳定"的行。
 *
 * 光看"自己多满"不够：合并标题带那种说明性文字本身可能很满，但它跟表头
 * 之间隔着空行，往下看不稳定，会被这条件排除。
 *
 * 光看"下面平均多满"也不够：MUJI 这种多层表头（英文名/日文名/说明行/系统
 * 代码逐层叠着），每一层自己都很满，用平均非空率打分，上面的表头层会因为
 * 往下还能看到别的表头层而被拉高分数，实测比真表头（下面是成片同密度的
 * 数据）分数还高。改成看"非空率的落差稳不稳"能避开这个坑：真表头下面是
 * 连续同密度的数据，落差趋近 0；表头堆内部往下看会混到别的表头层，密度
 * 忽高忽低，落差明显更大——这正是上面 STABILITY_THRESHOLD 注释里那两个
 * 数字（0.3186 对 0）的来源。
 *
 * 用"第一个满足条件就返回"而不是"打分取最高"，是为了防一类反例：如果数据
 * 本身比表头更满（表头里有空列名，数据行反而每列都填了），打分法会因为数据
 * 行自己的非空数更高而反超表头；返回第一个满足条件的行从一开始就锁定表头，
 * 不会被后面更满的数据行抢走。
 */
export function detectHeaderRow(rows: string[][]): number {
  const limit = Math.min(rows.length, DETECT_SCAN_ROWS)

  for (let i = 0; i < limit; i++) {
    const width = Math.max(rows[i].length, 1)
    const nonEmpty = rows[i].filter((cell) => cell.trim() !== '').length
    if (nonEmpty === 0) continue

    const lookahead = rows.slice(i + 1, i + 1 + LOOKAHEAD_ROWS)
    if (lookahead.length === 0) continue

    const fillRates = lookahead.map(
      (row) => row.filter((cell) => cell.trim() !== '').length / width,
    )
    const spread = Math.max(...fillRates) - Math.min(...fillRates)
    if (spread <= STABILITY_THRESHOLD) return i + 1
  }

  return 1
}
