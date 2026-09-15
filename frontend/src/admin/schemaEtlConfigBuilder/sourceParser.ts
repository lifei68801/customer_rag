/**
 * 前端唯一的表格解析实现。
 *
 * 这个文件之前是两份：tableHeader.ts 只读表头给表格导入页用，
 * guidedOntology/columnStats.ts 里另有一份私有的 readTableRows /
 * readExcelRows / readDelimitedRows 扫全表做列统计。两份都写死"第一个
 * sheet、第一行表头"，而且规则会悄悄分叉——2026-09-15 的 9c71cf9 让它们
 * 跟后端分叉过一次（后端给重名列加了 " (2)" 后缀，前端没有，结果用户在
 * 界面上选的列名跟跑批真正使用的列名指向不同的列）。现在只剩这一份。
 */

/** 解析选项。跟后端 SourceParseOptions 同义，字段名改成 camelCase。 */
export interface SourceParseOptions {
  /** 工作表名或 0-based 序号；不传表示第一个。只对 xlsx/xls 有意义。 */
  sheet?: string | number
  /** 表头所在行，1-based，缺省 1。 */
  headerRow?: number
  /** 首个数据行，1-based，缺省 headerRow + 1。 */
  firstDataRow?: number
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
 * 给重名列加 " (2)"、" (3)" 后缀，让每一列都有自己的键。
 *
 * 这是后端 `app/graphrag/etl_staging.py::deduplicate_header` 的对译，两边
 * 必须逐条一致——不一致时，用户在界面上选的列名跟跑批真正使用的列名会指向
 * 不同的列，而且看不出来。判据在 fixtures/header-dedup-cases.json，前后端
 * 测试各读一遍。
 *
 * 空列名彼此也算重名，同样参与去重。
 */
export function deduplicateHeader(names: string[]): string[] {
  const seen = new Set<string>()
  const result: string[] = []
  for (const name of names) {
    let candidate = name
    let suffix = 1
    // 表头里可能本来就有一列叫 "Color (2)"，所以不能算出后缀就直接用。
    while (seen.has(candidate)) {
      suffix += 1
      candidate = `${name} (${suffix})`
    }
    seen.add(candidate)
    result.push(candidate)
  }
  return result
}

// 按标准 CSV 引号规则（RFC 4180）解析一行，跟后端 Python csv 模块的解析规则
// 对齐——如果表头列名里本身带分隔符，必须用双引号包裹（如 "A,B"），双引号
// 内部的字面双引号写成两个连续双引号（""）转义，这里同样处理这两种情况。
// TSV 复用同一套引号规则，只是把逗号换成传入的 delimiter。
export function parseDelimitedHeaderLine(line: string, delimiter: string): string[] {
  const columns: string[] = []
  let current = ''
  let inQuotes = false
  for (let i = 0; i < line.length; i++) {
    const char = line[i]
    if (inQuotes) {
      if (char === '"') {
        if (line[i + 1] === '"') {
          current += '"'
          i++
        } else {
          inQuotes = false
        }
      } else {
        current += char
      }
    } else if (char === '"') {
      inQuotes = true
    } else if (char === delimiter) {
      columns.push(current)
      current = ''
    } else {
      current += char
    }
  }
  columns.push(current)
  return columns.map((c) => c.trim())
}

const EXCEL_EXTENSIONS = ['.xlsx', '.xls']

function extensionOf(file: File): string {
  return file.name.slice(file.name.lastIndexOf('.')).toLowerCase()
}

function isExcel(file: File): boolean {
  return EXCEL_EXTENSIONS.includes(extensionOf(file))
}

function delimiterOf(file: File): string {
  return extensionOf(file) === '.tsv' ? '\t' : ','
}

function headerRowOf(options: SourceParseOptions): number {
  return options.headerRow ?? 1
}

function firstDataRowOf(options: SourceParseOptions): number {
  return options.firstDataRow ?? headerRowOf(options) + 1
}

/**
 * 动态导入而不是顶层 import：SheetJS 压缩后接近 500KB，前台聊天页和后台
 * 管理页共享同一份打包产物（App.tsx 没有对路由做代码分割），静态 import
 * 会让只访问聊天页的普通用户也下载这个库。动态 import 让 Vite 把它拆成
 * 独立 chunk，只有真正解析 Excel 时才会触发下载。
 */
async function loadXlsx() {
  return import('xlsx')
}

/**
 * 选中工作表；选不中返回 null。
 *
 * 调用方必须报错，**不要**回落到第一张表——理由跟后端 `_select_xlsx_sheet`
 * 一样：回落会让用户拿到一份完全不相干的数据，而且不报错。
 */
function selectSheetName(
  workbook: { SheetNames: string[] },
  sheet: string | number | undefined,
): string | null {
  if (sheet === undefined) return workbook.SheetNames[0] ?? null
  if (typeof sheet === 'number') return workbook.SheetNames[sheet] ?? null
  return workbook.SheetNames.includes(sheet) ? sheet : null
}

function sheetNotFoundError(sheet: string | number, available: string[]): Error {
  return new Error(
    `找不到工作表 ${JSON.stringify(sheet)}，这个文件里有：${JSON.stringify(available)}`,
  )
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
 *
 * 产出物理行。**不支持跨行的引号字段**：带引号的字段里如果有换行，这里会
 * 把它切成两行，而后端（Python csv 模块）把它当成一条记录——两边行号和字段
 * 都会对不上。跑批前的后端对账会在真出现这种文件时把它报出来。
 */
async function* readDelimitedLines(file: File): AsyncGenerator<string> {
  const decoder = new TextDecoder('utf-8')
  let pending = ''
  let offset = 0
  while (offset < file.size) {
    const slice = file.slice(offset, offset + TEXT_CHUNK_BYTES)
    const buffer = await slice.arrayBuffer()
    pending += decoder.decode(buffer, { stream: true })
    offset += TEXT_CHUNK_BYTES

    let newlineMatch = pending.match(/\r\n|\r|\n/)
    while (newlineMatch && newlineMatch.index !== undefined) {
      yield pending.slice(0, newlineMatch.index)
      pending = pending.slice(newlineMatch.index + newlineMatch[0].length)
      newlineMatch = pending.match(/\r\n|\r|\n/)
    }
  }
  pending += decoder.decode()
  if (pending !== '') yield pending
}

/** 读一个工作簿里全部工作表的名字。非 Excel 文件没有工作表，返回空数组。 */
export async function listSheetNames(file: File): Promise<string[]> {
  if (!isExcel(file)) return []
  const XLSX = await loadXlsx()
  const buffer = await file.arrayBuffer()
  // sheetRows: 1——只要表名，不用把每张表的内容都解析出来。
  const workbook = XLSX.read(buffer, { type: 'array', sheetRows: 1 })
  return [...workbook.SheetNames]
}

/**
 * 逐行读一张表，把表头和每一行交给回调。
 *
 * 行跳过语义（CSV 与 Excel 共用）：表头取第 `headerRow` 行（1-based，缺省
 * 1），数据从 `firstDataRow ?? headerRow + 1` 行起。表头行号超出文件长度时
 * 什么回调都不触发，跟"空文件"同一个终态。
 *
 * CSV 侧按物理行计行号。**当前实现不支持跨行的引号字段**（后端支持），
 * 见 readDelimitedLines 的说明。
 */
export async function readSourceRows(
  file: File,
  options: SourceParseOptions,
  onHeader: (columns: string[]) => void,
  onRow: (row: string[]) => void,
): Promise<void> {
  if (isExcel(file)) {
    await readExcelRows(file, options, onHeader, onRow)
    return
  }
  await readDelimitedRows(file, delimiterOf(file), options, onHeader, onRow)
}

async function readDelimitedRows(
  file: File,
  delimiter: string,
  options: SourceParseOptions,
  onHeader: (columns: string[]) => void,
  onRow: (row: string[]) => void,
): Promise<void> {
  const headerRow = headerRowOf(options)
  const firstDataRow = firstDataRowOf(options)
  let lineNumber = 0
  for await (const line of readDelimitedLines(file)) {
    lineNumber += 1
    if (lineNumber < headerRow) continue
    if (lineNumber === headerRow) {
      onHeader(deduplicateHeader(parseDelimitedHeaderLine(line, delimiter)))
      continue
    }
    if (lineNumber < firstDataRow) continue
    // 跳过完全空白的行（比如文件末尾的换行符），但不跳过"看起来空但有
    // 分隔符"的行——那是真实的空值行，短行补齐由调用方处理。
    if (line === '') continue
    onRow(parseDelimitedHeaderLine(line, delimiter))
  }
}

async function readExcelRows(
  file: File,
  options: SourceParseOptions,
  onHeader: (columns: string[]) => void,
  onRow: (row: string[]) => void,
): Promise<void> {
  if (file.size > MAX_XLSX_BYTES) {
    throw new Error(
      `xlsx 文件过大（${file.size} 字节，上限 ${MAX_XLSX_BYTES} 字节）：xlsx 是二进制容器格式，` +
        '必须整个读进内存才能解析，文件太大会让浏览器卡死。请换一个更小的文件，或导出为 CSV。',
    )
  }
  const XLSX = await loadXlsx()
  const buffer = await file.arrayBuffer()
  // cellDates: true——不传的话 SheetJS 默认把日期格式的单元格读成 Excel
  // 内部的浮点序列号（比如 45678），不是 JS Date。那样 cellToString 里
  // `cell instanceof Date` 分支永远不命中，日期列会被 DATE_PATTERN 判不
  // 通过，退化成按整数/字符串处理——不报错，只是"下单日期"这种列悄悄
  // 不再被认成日期列，后续按日期列做的范围过滤处理也就用不上了。
  const workbook = XLSX.read(buffer, { type: 'array', cellDates: true })
  const sheetName = selectSheetName(workbook, options.sheet)
  if (sheetName === null) {
    if (options.sheet === undefined) return
    throw sheetNotFoundError(options.sheet, workbook.SheetNames)
  }
  const sheet = workbook.Sheets[sheetName]
  const rows = XLSX.utils.sheet_to_json<unknown[]>(sheet, { header: 1 })
  const headerRow = headerRowOf(options)
  const header = rows[headerRow - 1]
  if (header === undefined) return
  onHeader(deduplicateHeader(header.map((cell) => cellToString(cell).trim())))
  for (let index = firstDataRowOf(options) - 1; index < rows.length; index++) {
    onRow((rows[index] ?? []).map(cellToString))
  }
}

/** 读一张表的列名。表头行号超出文件长度时返回空数组。 */
export async function readSourceHeader(
  file: File,
  options: SourceParseOptions = {},
): Promise<string[]> {
  if (isExcel(file)) return readExcelHeader(file, options)
  return readDelimitedHeader(file, delimiterOf(file), options)
}

async function readDelimitedHeader(
  file: File,
  delimiter: string,
  options: SourceParseOptions,
): Promise<string[]> {
  const headerRow = headerRowOf(options)
  let lineNumber = 0
  // 只读到表头那一行就停：generator 是惰性的，后面的分块不会再去取。
  for await (const line of readDelimitedLines(file)) {
    lineNumber += 1
    if (lineNumber === headerRow) {
      return deduplicateHeader(parseDelimitedHeaderLine(line, delimiter))
    }
  }
  return []
}

async function readExcelHeader(file: File, options: SourceParseOptions): Promise<string[]> {
  const XLSX = await loadXlsx()
  const buffer = await file.arrayBuffer()
  const headerRow = headerRowOf(options)
  // sheetRows 限制只解析到表头那一行，不用把整个工作簿解析出来。
  const workbook = XLSX.read(buffer, { type: 'array', sheetRows: headerRow })
  const sheetName = selectSheetName(workbook, options.sheet)
  if (sheetName === null) {
    if (options.sheet === undefined) return []
    throw sheetNotFoundError(options.sheet, workbook.SheetNames)
  }
  const sheet = workbook.Sheets[sheetName]
  const rows = XLSX.utils.sheet_to_json<unknown[]>(sheet, { header: 1 })
  const header = rows[headerRow - 1]
  if (header === undefined) return []
  return deduplicateHeader(header.map((cell) => cellToString(cell).trim()))
}

/**
 * 原始的前若干行，**不受 headerRow 影响**——探测和预览要看的就是文件原始
 * 的形状，包括表头之上的标题带。不做重名去重，因为这里给的不是列名。
 */
export async function readSourcePreview(file: File, rowCount: number): Promise<string[][]> {
  if (rowCount <= 0) return []
  if (isExcel(file)) return readExcelPreview(file, rowCount)
  const delimiter = delimiterOf(file)
  const rows: string[][] = []
  for await (const line of readDelimitedLines(file)) {
    rows.push(parseDelimitedHeaderLine(line, delimiter))
    if (rows.length >= rowCount) break
  }
  return rows
}

async function readExcelPreview(file: File, rowCount: number): Promise<string[][]> {
  const XLSX = await loadXlsx()
  const buffer = await file.arrayBuffer()
  const workbook = XLSX.read(buffer, { type: 'array', cellDates: true, sheetRows: rowCount })
  const sheetName = selectSheetName(workbook, undefined)
  if (sheetName === null) return []
  const sheet = workbook.Sheets[sheetName]
  const rows = XLSX.utils.sheet_to_json<unknown[]>(sheet, { header: 1 })
  return rows.slice(0, rowCount).map((row) => (row ?? []).map((cell) => cellToString(cell).trim()))
}
