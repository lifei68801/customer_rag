import { readSourceRows } from '../schemaEtlConfigBuilder/sourceParser'
import type { SourceParseOptions } from '../schemaEtlConfigBuilder/sourceParser'
import type { ColumnStats, InferredType } from './types'

/**
 * 解析实现在 schemaEtlConfigBuilder/sourceParser.ts，这里只做统计。
 *
 * 这两个常量继续从这里导出：既有测试引用的是这个模块的符号，而它们描述的
 * 是解析行为，定义已经搬到 sourceParser。
 */
export { MAX_XLSX_BYTES, TEXT_CHUNK_BYTES } from '../schemaEtlConfigBuilder/sourceParser'

/**
 * 每列最多收集这么多不同值，超过就封顶。
 *
 * 判定只需要知道"低基数还是高基数"，不需要精确数字。不封顶的话，一张
 * 百万行的表会把每列的所有值都留在内存里。
 */
export const DISTINCT_LIMIT = 1000

/** 样例值给用户看，不需要多。 */
const SAMPLE_LIMIT = 5

/**
 * 高于这个基数的整数列一律当字符串。
 *
 * 高基数的整数几乎总是标识（订单号、SKU），不是度量。判成 number 会让它
 * 被归进属性，那个实体类型就整个没了——而这不会报错。
 */
const NUMERIC_IDENTIFIER_THRESHOLD = 50

interface ColumnAccumulator {
  name: string
  nonEmptyCount: number
  distinct: Set<string>
  capped: boolean
  sawNonNumeric: boolean
  sawFraction: boolean
  sawNonDate: boolean
  sawAnyValue: boolean
}

export interface StatsAccumulator {
  columns: ColumnAccumulator[]
}

const DATE_PATTERN = /^\d{4}[-/]\d{1,2}[-/]\d{1,2}([ T].*)?$/

/**
 * 单值检测最多算这么多对（宿主 × 属性）。
 *
 * 每一对要维护一个「宿主值 → 第一次见到的属性值」的 Map，内存随对数线性
 * 增长。300 对够覆盖真实业务表（十来个维度列 × 十来个度量列）；超过之后
 * 宁可如实说「没检测」，也不要为了给个结论而去采样——采样得到的「没发现
 * 冲突」和真的没冲突在界面上长得一模一样。
 */
export const MAX_PAIRS = 300

/** 一个宿主值对应了多个属性值——导入后它会变成一条待决议冲突。 */
export interface PairViolation {
  /** 是哪个宿主值，比如客户 'A'。 */
  hostValue: string
  /** 这个宿主值出现在多少行里（属性非空的那些行）。 */
  rowCount: number
  /** 这些行里属性有几个不同的取值。 */
  distinctCount: number
  /** 其中几个取值，给用户认。 */
  samples: string[]
}

export interface PairReport {
  /** 对数超过 MAX_PAIRS，整个检测没做。**这不等于没冲突。** */
  skipped: boolean
  violationOf(hostColumn: string, attributeColumn: string): PairViolation | null
}

/** 每对样例最多留这么多个不同取值。 */
const VIOLATION_SAMPLE_LIMIT = 3

interface PairAccumulator {
  hostIndex: number
  attributeIndex: number
  /** 宿主值 → 见过的属性取值。一旦这一对已经确认冲突就不再增长。 */
  seen: Map<string, Set<string>>
  /** 宿主值 → 属性非空的行数。 */
  rows: Map<string, number>
  violation: PairViolation | null
}

export interface PairsAccumulator {
  skipped: boolean
  pairs: PairAccumulator[]
  keyOf: Map<string, PairAccumulator>
}

const pairKey = (host: string, attribute: string) => `${host}\u0000${attribute}`

export function createPairsAccumulator(options: {
  columns: string[]
  hostColumns: string[]
  attributeColumns: string[]
}): PairsAccumulator {
  const { columns, hostColumns, attributeColumns } = options
  const indexOf = new Map(columns.map((name, index) => [name, index]))
  const pairs: PairAccumulator[] = []
  const keyOf = new Map<string, PairAccumulator>()
  if (hostColumns.length * attributeColumns.length > MAX_PAIRS) {
    return { skipped: true, pairs, keyOf }
  }
  for (const host of hostColumns) {
    for (const attribute of attributeColumns) {
      const hostIndex = indexOf.get(host)
      const attributeIndex = indexOf.get(attribute)
      if (hostIndex === undefined || attributeIndex === undefined) continue
      // 一列挂到它自己身上没有意义，跳过。
      if (hostIndex === attributeIndex) continue
      const pair: PairAccumulator = {
        hostIndex,
        attributeIndex,
        seen: new Map(),
        rows: new Map(),
        violation: null,
      }
      pairs.push(pair)
      keyOf.set(pairKey(host, attribute), pair)
    }
  }
  return { skipped: false, pairs, keyOf }
}

export function accumulatePairRow(acc: PairsAccumulator, row: string[]): void {
  if (acc.skipped) return
  for (const pair of acc.pairs) {
    const hostValue = (row[pair.hostIndex] ?? '').trim()
    const attributeValue = (row[pair.attributeIndex] ?? '').trim()
    // 两端任一为空都不参与判定：一个客户有一行没填等级，不是"两个不同的
    // 等级"，只是这一行没有这个信息。
    if (hostValue === '' || attributeValue === '') continue
    pair.rows.set(hostValue, (pair.rows.get(hostValue) ?? 0) + 1)
    let values = pair.seen.get(hostValue)
    if (values === undefined) {
      values = new Set()
      pair.seen.set(hostValue, values)
    }
    values.add(attributeValue)
  }
}

export function finalizePairs(acc: PairsAccumulator): PairReport {
  if (acc.skipped) {
    return { skipped: true, violationOf: () => null }
  }
  for (const pair of acc.pairs) {
    for (const [hostValue, values] of pair.seen) {
      if (values.size <= 1) continue
      // 报第一个撞上的宿主值就够：用户要的是"这件事会发生"和一个能去核对
      // 的具体例子，不是全部反例的清单。
      pair.violation = {
        hostValue,
        rowCount: pair.rows.get(hostValue) ?? values.size,
        distinctCount: values.size,
        samples: [...values].slice(0, VIOLATION_SAMPLE_LIMIT),
      }
      break
    }
  }
  return {
    skipped: false,
    violationOf: (host, attribute) => acc.keyOf.get(pairKey(host, attribute))?.violation ?? null,
  }
}

/**
 * 一次算完：给定全部行，判每一对（宿主列，属性列）是不是单值。
 *
 * 属性挂到**非中心**实体上时才需要这个判断。中心每行一个实例，天然单值；
 * 而「客户等级」挂到「客户」上时，同一个客户在多行里如果等级不一样，
 * 导入后就会变成一条待决议的属性冲突——那件事该在建模时就让用户知道，
 * 不是导完才发现。
 */
export function detectSingleValued(options: {
  columns: string[]
  rows: string[][]
  hostColumns: string[]
  attributeColumns: string[]
}): PairReport {
  const acc = createPairsAccumulator(options)
  for (const row of options.rows) accumulatePairRow(acc, row)
  return finalizePairs(acc)
}

export function createAccumulator(columns: string[]): StatsAccumulator {
  return {
    columns: columns.map((name) => ({
      name,
      nonEmptyCount: 0,
      distinct: new Set<string>(),
      capped: false,
      sawNonNumeric: false,
      sawFraction: false,
      sawNonDate: false,
      sawAnyValue: false,
    })),
  }
}

export function accumulateRow(acc: StatsAccumulator, row: string[]): void {
  acc.columns.forEach((column, index) => {
    // 短行按空值补齐：CSV 里尾部空列常被省略，报错会让引导卡在第一步。
    const raw = (row[index] ?? '').trim()
    if (raw === '') return
    column.nonEmptyCount += 1
    column.sawAnyValue = true
    if (!column.capped) {
      column.distinct.add(raw)
      if (column.distinct.size >= DISTINCT_LIMIT) column.capped = true
    }
    if (!/^-?\d+(\.\d+)?$/.test(raw)) column.sawNonNumeric = true
    else if (raw.includes('.')) column.sawFraction = true
    if (!DATE_PATTERN.test(raw)) column.sawNonDate = true
  })
}

function inferType(column: ColumnAccumulator): InferredType {
  if (!column.sawAnyValue) return 'string'
  if (!column.sawNonDate) return 'date'
  if (column.sawNonNumeric) return 'string'
  // 高基数的整数列是标识，不是度量。
  if (!column.sawFraction && (column.capped || column.distinct.size > NUMERIC_IDENTIFIER_THRESHOLD)) {
    return 'string'
  }
  return column.sawFraction ? 'number' : 'integer'
}

export function finalizeStats(acc: StatsAccumulator): ColumnStats[] {
  return acc.columns.map((column) => ({
    name: column.name,
    nonEmptyCount: column.nonEmptyCount,
    distinctCount: column.capped ? DISTINCT_LIMIT : column.distinct.size,
    distinctCapped: column.capped,
    samples: [...column.distinct].slice(0, SAMPLE_LIMIT),
    inferredType: inferType(column),
    // 原始观察，不受 inferType 的改判影响：见 types.ts 里 isWholeNumber 的说明。
    isWholeNumber: column.sawAnyValue && !column.sawNonNumeric && !column.sawFraction,
  }))
}

/**
 * 扫描整个文件，产出每列统计量。文件不上传——建模阶段数据不出用户的机器。
 *
 * 明确**不采样**：前 N 行不是随机样本。订单表通常按时间排序，前 1000 行
 * 可能只有 3 个州，基数估计会严重偏低，把本该是实体的列判成属性——而那
 * 不会报错，只会让本体建歪。
 *
 * xlsx 必须整个读进内存（二进制容器格式没法只读一段），所以对它加了体积
 * 上限；超过就抛错并说明原因，不能让页面静静地卡住。
 */
export async function scanTableFile(
  file: File,
  options: SourceParseOptions = {},
): Promise<ColumnStats[]> {
  let acc: StatsAccumulator | null = null
  await readSourceRows(
    file,
    options,
    (columns) => {
      acc = createAccumulator(columns)
    },
    (row) => {
      if (acc !== null) accumulateRow(acc, row)
    },
  )
  if (acc === null) return []
  return finalizeStats(acc)
}

/**
 * 第二遍：算每一对（候选宿主列，属性列）是不是单值。
 *
 * 在 assignRoles 之后调用——那时才知道哪些列是宿主、哪些是属性，只算真正
 * 需要的那些对，而不是所有列两两配对。
 */
export async function scanPairs(
  file: File,
  columnRoles: { hostColumns: string[]; attributeColumns: string[] },
  options: SourceParseOptions = {},
): Promise<PairReport> {
  let acc: PairsAccumulator | null = null
  await readSourceRows(
    file,
    options,
    (columns) => {
      acc = createPairsAccumulator({ columns, ...columnRoles })
    },
    (row) => {
      if (acc !== null) accumulatePairRow(acc, row)
    },
  )
  if (acc === null) return { skipped: false, violationOf: () => null }
  return finalizePairs(acc)
}

