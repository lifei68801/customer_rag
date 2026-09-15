/** 只看前这么多行。表头不可能在第 20 行以后，多扫只是浪费。 */
export const DETECT_SCAN_ROWS = 20

/**
 * 判定"从这一行开始，连续这么多行非空格子数都很接近"要看多少行，才能确认
 * 这是真正的数据块，而不是一段巧合很整齐的垃圾行。选 7 是因为要盖过"连续
 * 5 行格子数相近的垃圾行"（信头/落款重复）——如果窗口跟垃圾行一样长（5
 * 行），垃圾行自己就会被误判成"稳定的数据块"；窗口留够 7 行，垃圾行内部任
 * 何一个起点都会在窗口末尾探到垃圾行外面的表头/数据，格子数跳变，测不出
 * "稳定"。用变异测试验证过：改回 5 会让"5 行垃圾行"那条用例选错。
 *
 * 证据强度：弱。这个数字只有"5 行垃圾行"这个自造用例撑着，7=5+2 是从这条
 * 用例反推出来的，不是从真实文件里测出来的。如果以后有真实文件推翻它，应该
 * 改的是这里，而不是把用例改得凑合这个数字。
 */
const RUN_WINDOW_ROWS = 7

/**
 * 窗口至少要有这么多行才采信"稳定"。只有 1 行的窗口，落差恒为 0，会把任何
 * 非空的孤行都误判成"稳定的数据块"——哪怕它后面根本没有数据能验证。
 *
 * 证据强度：这是逻辑下限（1 行窗口的落差恒为 0，不携带任何信息），由"候选
 * 行后面没有行可验证"用例锁住，不依赖真实数据。
 */
const MIN_RUN_CONFIRM = 2

/**
 * 窗口内非空格子数的落差（最大 − 最小）不超过 max(TOLERANCE_CELLS,
 * SPREAD_RATIO × 窗口内最大格子数) 就算"稳定"。
 *
 * SPREAD_RATIO = 0.2 的证据强度分两头说：
 * - 上限有真实数据：真实 MUJI 文件（113 列）从第 3 行起的窗口是
 *   [107, 92, 105, 108, 72, 72, 72]，落差 36、最大值 108，比例 0.333；从
 *   第 4/5/6 行起的窗口落差同样是 36（最大值分别是 108/108/108）。比例必
 *   须小于 36/108 才不会把表头堆误判成数据块——0.2 离这个上限有余量。
 * - 下限只有合成用例："数据本身参差不齐"用例（10 列，数据在 8~10 格之间
 *   波动）的落差是 2、最大值 10，恰好等于 0.2。也就是说 0.2 正好落在这条
 *   自造用例的边界上，是从用例反推的，没有真实文件证明数据行的自然波动
 *   就是 20%。
 *
 * TOLERANCE_CELLS = 1 的证据强度：只有合成用例。窄表（2 列、3 列）里一个
 * 格子的有无就是 33%~50% 的比例落差，按比例判定对窄表不成立；"容忍 1 个
 * 格子"由"窄表缺一格"（2 列）和"数据比表头更满"（3 列表头少填 1 格）两条
 * 自造用例锁住，没有真实窄表文件验证过。
 *
 * 比较时直接用浮点 `SPREAD_RATIO * hi`，不加 epsilon：落差和最大值都是整数，
 * 已经穷举核对过 hi ∈ [0, 10^7] 内 `s <= Math.max(1, 0.2 * hi)` 与精确判定
 * `s <= 1 || 5 * s <= hi` 处处一致（5 的倍数上 0.2 * hi 精确等于 hi / 5）。
 * 旧版按密度比较时的 `1 - 2/3 > 1/3` 浮点问题来自两个分数相减，整数计数框
 * 架下不存在。
 */
const TOLERANCE_CELLS = 1
const SPREAD_RATIO = 0.2

/**
 * 数一行里的非空格子。不除以任何宽度：Excel 经 SheetJS 解析后每行只保留到
 * "本行最后一个非空格子"，行长参差不齐（真实 MUJI 前 5 行的 length 是
 * 101/97/113/100/113）；而取所有行的最大 length 当统一宽度又会被一个远端
 * 孤立单元格（比如某行第 200 列的备注）撑大，把整张表的密度一起压扁。"表
 * 格有多宽"没有可靠定义，所以干脆不用。
 *
 * `cell !== undefined`：SheetJS 的 `sheet_to_json(..., { header: 1 })` 不传
 * `defval` 时，行数组中间的空格子是稀疏数组的空洞（`1 in row === false`），
 * 上游 `.map(cellToString)` 会原样保留空洞，按下标读出来就是 undefined。
 */
function countNonEmpty(row: string[]): number {
  let count = 0
  for (let i = 0; i < row.length; i++) {
    const cell = row[i]
    if (cell !== undefined && cell.trim() !== '') count++
  }
  return count
}

/** counts 从 start 起、至多 RUN_WINDOW_ROWS 行的窗口是否稳定。 */
function isStableRun(counts: number[], start: number): boolean {
  const stop = Math.min(counts.length, start + RUN_WINDOW_ROWS)
  if (stop - start < MIN_RUN_CONFIRM) return false

  let lo = counts[start]
  let hi = counts[start]
  for (let i = start + 1; i < stop; i++) {
    if (counts[i] < lo) lo = counts[i]
    if (counts[i] > hi) hi = counts[i]
  }
  return hi - lo <= Math.max(TOLERANCE_CELLS, SPREAD_RATIO * hi)
}

/**
 * 猜表头在第几行（1-based）。
 *
 * **这只是建议，永远不自动生效。** 界面要显示"猜的是第 N 行"并让用户改——
 * 推断提议、人确认，跟 Foundry 的 schema 推断对话框是同一个姿态。自动生效
 * 的推断一旦猜错，用户看到的是一份莫名其妙的数据，而不是一个可以改的选项。
 *
 * 只比较每行的非空格子数，分两步走：
 *
 * 1. 在前 DETECT_SCAN_ROWS 行里从上往下找第一个数据块起点 runStart：这一
 *    行自身非空，且从它开始的 RUN_WINDOW_ROWS 行格子数落差在容差以内。起
 *    点必须在扫描区内，但窗口可以伸出扫描区去确认稳定性（表头在第 20 行时，
 *    它的数据在第 21 行以后）；再往后的行一概不读，所以扫描区外的内容、以
 *    及表格总行数都不影响结果。以全空行开头的窗口不算——一段全空行落差为
 *    0，但它不是数据。
 * 2. 数据块的起点不一定就是表头：如果它前一行的格子数不少于起点行（比
 *    如 MUJI 第 6 行系统代码 108 格，数据 72 格），前一行才是表头，往前挪
 *    一格；否则起点自己就是表头。只挪一格不递归——多层表头堆里只有紧贴数
 *    据块的那一层是真表头（MUJI 第 3~5 行也比数据满，但不该选）。
 *
 * 全程没有要求"表头一定比数据更满"或"一定一样满"：前者（MUJI）靠第 2 步
 * 命中，后者的反例（表头留了空列名，比数据少 1 格）落在第 1 步的容差里，
 * 表头直接就是数据块起点。
 *
 * 已知局限：
 *
 * - 窄表里"标题行只填了半行"和"数据行缺了一格"是同一个数字：2 列表里两
 *   者都是 1 格，本函数分不出来。这是纯计数信号的天花板。
 * - 窄表里两处独立的 1 格异常叠加会超出容差。例如 2 列表，某数据行缺一格
 *   （1 格）、另一数据行在远端多出一个孤立单元格（3 格），窗口落差就是 2，
 *   超过"容忍 1 格"，跨过两处异常的窗口都不算稳定，数据块起点被推到后面
 *   （实测这个形状返回第 4 行，即带孤立单元格的那一行，而不是第 1 行）。
 *   接受这个代价，是因为反过来按宽度算密度时，宽表数据行远端一个孤立备注
 *   单元格就能把真实 MUJI 形状打错（上一版实现实测返回第 3 行或第 1 行）。
 *   取舍依据是判断而非统计：宽表数据行远端挂一个孤立备注，被认为比窄表同
 *   一窗口里恰好叠加两种异常常见得多——没有拿真实文件集合数过。把容差放
 *   宽到 2 格也不是出路：那样 2 列表里任何以非空行开头的窗口都算"稳定"，
 *   窄表就完全没有判别力了。
 */
export function detectHeaderRow(rows: string[][]): number {
  const limit = Math.min(rows.length, DETECT_SCAN_ROWS)
  // 起点最远是 limit - 1，它的窗口最远读到 limit - 1 + RUN_WINDOW_ROWS - 1。
  const end = Math.min(rows.length, limit + RUN_WINDOW_ROWS - 1)
  const counts: number[] = []
  for (let i = 0; i < end; i++) counts.push(countNonEmpty(rows[i]))

  let runStart = -1
  for (let r = 0; r < limit; r++) {
    if (counts[r] === 0) continue
    if (isStableRun(counts, r)) {
      runStart = r
      break
    }
  }

  if (runStart === -1) return 1

  // 不必再单独要求前一行非空：数据块起点本身非空（counts[runStart] > 0），
  // "前一行格子数不少于它"已经蕴含前一行非空。
  if (runStart > 0 && counts[runStart - 1] >= counts[runStart]) return runStart

  return runStart + 1
}
