import { parseDelimitedHeaderLine } from '../schemaEtlConfigBuilder/tableHeader'
import type { ColumnStats, InferredType } from './types'

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
  }))
}

/**
 * xlsx 的体积上限。它必须整个读进内存再解析，超过这个量级浏览器会卡死。
 * CSV 走流式读取，不受这个限制。
 */
export const MAX_XLSX_BYTES = 20 * 1024 * 1024

/**
 * CSV/TSV 分块读取的块大小。按字节切片，不按行——文件多大都只占这一块内存。
 *
 * 导出给测试用，好让测试精确控制"一行/一个多字节字符正好切在块边界上"
 * 这种场景，而不用去猜实现里的常量。
 */
export const TEXT_CHUNK_BYTES = 1024 * 1024

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
export async function scanTableFile(file: File): Promise<ColumnStats[]> {
  const extension = file.name.slice(file.name.lastIndexOf('.')).toLowerCase()
  if (extension === '.xlsx' || extension === '.xls') {
    return scanExcelFile(file)
  }
  const delimiter = extension === '.tsv' ? '\t' : ','
  return scanDelimitedFile(file, delimiter)
}

async function scanExcelFile(file: File): Promise<ColumnStats[]> {
  if (file.size > MAX_XLSX_BYTES) {
    throw new Error(
      `xlsx 文件过大（${file.size} 字节，上限 ${MAX_XLSX_BYTES} 字节）：xlsx 是二进制容器格式，` +
        '必须整个读进内存才能解析，文件太大会让浏览器卡死。请换一个更小的文件，或导出为 CSV。',
    )
  }
  // 动态导入而不是顶层 import：SheetJS 压缩后接近 500KB，理由同
  // tableHeader.ts 里的 readExcelHeaderColumns——只有真正扫描 Excel 时才
  // 应该触发下载，不能拖累所有页面的首屏包体积。
  const XLSX = await import('xlsx')
  const buffer = await file.arrayBuffer()
  // cellDates: true——不传的话 SheetJS 默认把日期格式的单元格读成 Excel
  // 内部的浮点序列号（比如 45678），不是 JS Date。那样 cellToString 里
  // `cell instanceof Date` 分支永远不命中，日期列会被 DATE_PATTERN 判不
  // 通过，退化成按整数/字符串处理——不报错，只是"下单日期"这种列悄悄
  // 不再被认成日期列，后续按日期列做的范围过滤处理也就用不上了。
  const workbook = XLSX.read(buffer, { type: 'array', cellDates: true })
  const firstSheetName = workbook.SheetNames[0]
  if (!firstSheetName) return []
  const sheet = workbook.Sheets[firstSheetName]
  const rows = XLSX.utils.sheet_to_json<unknown[]>(sheet, { header: 1 })
  const [headerRow, ...dataRows] = rows
  const columns = (headerRow ?? []).map((cell) => cellToString(cell).trim())
  const acc = createAccumulator(columns)
  for (const row of dataRows) {
    accumulateRow(acc, row.map(cellToString))
  }
  return finalizeStats(acc)
}

function cellToString(cell: unknown): string {
  if (cell === undefined || cell === null) return ''
  if (cell instanceof Date) return cell.toISOString().slice(0, 10)
  return String(cell)
}

/**
 * CSV/TSV 按字节分块读取，不把整个文件读进内存(不 `await file.text()`)。
 * jsdom 和不少运行环境下 `File.prototype.stream()` 不可用（测试环境里
 * 就没有），所以用 `Blob.slice()` 按固定字节数递进，配合一个持续存活的
 * `TextDecoder` 实例（`{ stream: true }`）——这样即使某次切片正好切在一个
 * 多字节 UTF-8 字符中间，解码器也会把半个字符缓存到下一块，不会产生乱码。
 */
async function scanDelimitedFile(file: File, delimiter: string): Promise<ColumnStats[]> {
  const decoder = new TextDecoder('utf-8')
  let pending = ''
  let columns: string[] | null = null
  let acc: StatsAccumulator | null = null

  const consumeLine = (line: string) => {
    // 跳过完全空白的行（比如文件末尾的换行符），但不跳过"看起来空但有
    // 分隔符"的行——那是真实的空值行，短行补齐已经在 accumulateRow 里处理。
    if (line === '') return
    const fields = parseDelimitedHeaderLine(line, delimiter)
    if (columns === null) {
      columns = fields.map((f) => f.trim())
      acc = createAccumulator(columns)
      return
    }
    accumulateRow(acc as StatsAccumulator, fields)
  }

  let offset = 0
  while (offset < file.size) {
    const slice = file.slice(offset, offset + TEXT_CHUNK_BYTES)
    const buffer = await slice.arrayBuffer()
    pending += decoder.decode(buffer, { stream: true })
    offset += TEXT_CHUNK_BYTES

    let newlineMatch = pending.match(/\r\n|\r|\n/)
    while (newlineMatch && newlineMatch.index !== undefined) {
      const line = pending.slice(0, newlineMatch.index)
      consumeLine(line)
      pending = pending.slice(newlineMatch.index + newlineMatch[0].length)
      newlineMatch = pending.match(/\r\n|\r|\n/)
    }
  }
  pending += decoder.decode()
  if (pending !== '') consumeLine(pending)

  if (columns === null || acc === null) return []
  return finalizeStats(acc)
}
