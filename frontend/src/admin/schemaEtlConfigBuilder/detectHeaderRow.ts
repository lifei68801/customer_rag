/** 只看前这么多行。表头不可能在第 20 行以后，多扫只是浪费。 */
export const DETECT_SCAN_ROWS = 20

/**
 * 判定"从这一行开始，连续这么多行密度都很接近"要看多少行，才能确认这是
 * 真正的数据块，而不是一段巧合很整齐的垃圾行。选 7 是因为要盖过"连续 5 行
 * 密度相近的垃圾行"（信头/落款重复）——如果窗口跟垃圾行一样长（5 行），
 * 垃圾行自己就会被误判成"稳定的数据块"；窗口留够 7 行，垃圾行内部任何一
 * 个起点都会在窗口末尾探到垃圾行外面的表头/数据，密度跳变，测不出"稳定"。
 * 用变异测试验证过：改回 5 会让"5 行垃圾行"那条用例选错。
 *
 * 这个数字目前只有"5 行垃圾行"这个自造用例撑着，7=5+2 是从这条用例反推
 * 出来的，不是从真实文件里测出来的——不像 BASE_STABILITY_THRESHOLD 那样
 * 有真实 MUJI 数字背书。如果以后有真实文件推翻它，应该改的是这里，而不是
 * 把用例改得凑合这个数字。
 */
const RUN_WINDOW_ROWS = 7

/**
 * 密度落差在多少以内算"稳定"，按列数放宽——列越少，一个格子的有无对密度
 * 的影响越大（2 列表缺一格，密度就跳 0.5），固定阈值对窄表不成立。
 * 0.2 是宽表的下限：用真实 MUJI 文件（113 列）核实过，真表头往后看的落差
 * 是 0（连续同密度数据），表头堆内部往下看落差是 0.3186，0.2 卡在中间留了
 * 安全余量。TOLERANCE_CELLS/width 是窄表的补充：等价于"容忍 1 个格子的
 * 有无"，用两条窄表用例验证过（2 列表缺 1 格、3 列表表头比数据少填 1 格）。
 */
const BASE_STABILITY_THRESHOLD = 0.2
const TOLERANCE_CELLS = 1

/**
 * 窗口至少要有这么多行才采信"稳定"。只有 1 行的窗口，落差恒为 0，会把任何
 * 非空的孤行都误判成"稳定的数据块"——哪怕它后面根本没有数据能验证。
 */
const MIN_RUN_CONFIRM = 2

/** 浮点数比较的容差，避免"容忍 1 个格子"这类算出来的边界值因为浮点运算误差被判定为不相等。 */
const STABILITY_EPSILON = 1e-9

function density(row: string[], width: number): number {
  return row.filter((cell) => cell.trim() !== '').length / width
}

/**
 * 表格的列数不能用某一行自己的 length 代表——Excel 经 SheetJS 解析后，每行
 * 只保留到"这一行最后一个非空格子"，行尾会被裁掉，行与行之间的 length 并
 * 不相等（真实 MUJI 文件前几行的 length 依次是 101/97/113/100/113，真实
 * 列数是 113）。如果拿窗口里某一行自己的 length 当分母去算别的行的密度，
 * 会把"这一行本身被裁短"误算成"别的行密度暴涨"（比如用长度 100 的行去除
 * 长度 113 的行的非空数，能算出超过 1 的"密度"）。用扫描区里出现过的最大
 * length 做统一分母——最长的那一行大概率没被裁掉，最接近真实列数。
 */
function tableWidth(rows: string[][]): number {
  return Math.max(1, ...rows.map((row) => row.length))
}

function stabilityThreshold(width: number): number {
  return Math.max(BASE_STABILITY_THRESHOLD, TOLERANCE_CELLS / width) + STABILITY_EPSILON
}

/**
 * 猜表头在第几行（1-based）。
 *
 * **这只是建议，永远不自动生效。** 界面要显示"猜的是第 N 行"并让用户改——
 * 推断提议、人确认，跟 Foundry 的 schema 推断对话框是同一个姿态。自动生效
 * 的推断一旦猜错，用户看到的是一份莫名其妙的数据，而不是一个可以改的选项。
 *
 * 分两步走：
 *
 * 1. 从上往下找第一个"连续 RUN_WINDOW_ROWS 行密度都很接近"的起点（runStart）
 *    ——这段就是数据块。只看"下一行"或"接下来 5 行的平均值"都不够：前者
 *    会把表头正上方那种密度恰好只差一点的说明行也当成表头（拿窄表当反例，
 *    实测会跟"表头本身就该缺一格"的情况分不清）；后者在 MUJI 这种多层表
 *    头文件上，会让表头堆的上层因为往下还能看到别的表头层而拿到虚高的分
 *    数。这里用"连续一段窗口本身够不够稳定"来判定数据块的起点，不掺候选
 *    行自己的密度。
 * 2. 数据块的起点不一定就是表头——如果它前面那一行本身非空、且密度不低于
 *    数据块（比如 MUJI 第 6 行系统代码，比数据更满），那一行才是表头，往
 *    前挪一格；否则数据块的起点自己就是表头（比如表头密度跟数据一致，或
 *    表头本身留了空列名、反而比数据稀的情况）。往前只挪一格，不递归再往
 *    前找——多层表头堆里，只有紧贴数据块的那一层是真表头，再往上的层（哪
 *    怕也比数据密）都不该被选中。
 *
 * 全程没有要求"表头一定比数据更满"或"表头一定跟数据一样满"——两种真实
 * 场景都存在（MUJI 是前者，表头留空列名反而比数据稀是后者的反例），所以
 * 第 2 步是尝试性地"往前挪"而不是硬性门槛。
 *
 * 已知局限：窄表（比如 2 列）下，"表头/说明行只填了半行"和"数据行缺了
 * 一格"在密度上完全等价，本函数分不出来——这是纯密度信号的天花板，不是
 * 实现疏漏。
 */
export function detectHeaderRow(rows: string[][]): number {
  const limit = Math.min(rows.length, DETECT_SCAN_ROWS)
  const width = tableWidth(rows)
  const threshold = stabilityThreshold(width)

  let runStart = -1
  for (let r = 0; r < limit; r++) {
    const window = rows.slice(r, r + RUN_WINDOW_ROWS)
    if (window.length < MIN_RUN_CONFIRM) continue

    const densities = window.map((row) => density(row, width))
    const spread = Math.max(...densities) - Math.min(...densities)
    if (spread <= threshold) {
      runStart = r
      break
    }
  }

  if (runStart === -1) return 1

  if (runStart > 0) {
    const prevRow = rows[runStart - 1]
    const prevNonEmpty = prevRow.filter((cell) => cell.trim() !== '').length
    if (prevNonEmpty > 0) {
      const runDensity = density(rows[runStart], width)
      if (density(prevRow, width) >= runDensity) return runStart
    }
  }

  return runStart + 1
}
