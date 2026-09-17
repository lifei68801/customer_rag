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

/**
 * 砍掉行尾那些名字为空的列。
 *
 * 四个读取器对"这张表有几列"的口径本来各不相同：xlrd（.xls）按实际存在的
 * 单元格记录算，openpyxl 的 read_only 模式按工作表声明的 dimension 算，
 * 这里的 SheetJS 按 `!ref` 算。手工编辑过的 Excel 里声明范围常常比实际数据
 * 宽，于是同一张表在两端得到不同的列数——真实的 MUJI .xls 是前端 114 /
 * 后端 113，而 dimension 被撑宽的 .xlsx 反过来是前端 3 / 后端 6。列数对不上，
 * 跑批前的逐列对账会直接判 400，这张表根本导不进来。
 *
 * 这条规则跟后端 `app/graphrag/etl_staging.py::trim_trailing_empty_names`
 * 逐字对应：取到表头行之后、去重之前，砍掉**行尾连续的**空名列。
 *
 * 只砍尾部：中间的空名列一列都不能动。MUJI 那张表第 52~56 列就是中间的
 * 空名列，砍掉会让后面所有列的位置整体左移——而列数还是对得上的，对账
 * 发现不了。
 *
 * 代价：行尾那些没有列名、下面却有数据的列会被丢掉。这类列今天本来也无法
 * 在字段映射里被引用（名字是空的），丢掉它换列数口径一致。
 */
export function trimTrailingEmptyNames(names: string[]): string[] {
  let end = names.length
  while (end > 0 && names[end - 1].trim() === '') end -= 1
  return names.slice(0, end)
}

/**
 * 原始表头 → 可用作列键的表头。两步的顺序是这一处说了算，四个读取入口都走
 * 它，免得某一条路径漏掉一步或把顺序做反：先砍尾部空名列，再去重。反过来的
 * 话 " (2)" 这类后缀是按砍之前的位置算出来的，砍掉之后编号会错乱。
 */
function headerFrom(rawNames: string[]): string[] {
  return deduplicateHeader(trimTrailingEmptyNames(rawNames))
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
 * 两份解析选项读出来的是不是同一批行。
 *
 * 比的是**补齐缺省之后**的值，不是字面值：用户在表头行输入框里把 1 原样打
 * 一遍，选项会从 `{}` 变成 `{ headerRow: 1 }`，可读的还是同一批行。缺省值
 * 怎么补由 headerRowOf / firstDataRowOf 说了算，跟三个读取入口同一处。
 */
/**
 * 两份选项指的是不是同一张工作表。不传和传 0 都是第一张表（selectSheetName
 * 里 `undefined` 取 SheetNames[0]、数字按下标取），字面比较会把它们判成改
 * 过，于是"解析设置有没有变"永远为真，每次跑批都多传一次 config。
 */
function sameSheet(a: SourceParseOptions, b: SourceParseOptions): boolean {
  const normalize = (sheet: string | number | undefined) => (sheet === undefined ? 0 : sheet)
  return normalize(a.sheet) === normalize(b.sheet)
}

export function sameEffectiveParseOptions(a: SourceParseOptions, b: SourceParseOptions): boolean {
  return (
    sameSheet(a, b) &&
    headerRowOf(a) === headerRowOf(b) &&
    firstDataRowOf(a) === firstDataRowOf(b)
  )
}

/**
 * 选项自身不合法时立刻报错，拒绝规则跟后端
 * `app/graphrag/source_parse_options.py::SourceParseOptions.__post_init__` 逐条对齐。
 *
 * 校验只写这一处、三个入口（readSourceRows / readSourceHeader /
 * readSourcePreview）都走它：CSV 路径和 Excel 路径对同一份非法输入会给出
 * 不同结果——`headerRow=2, firstDataRow=1` 时 CSV 路径靠 `lineNumber < headerRow`
 * 把表头之前的行挡掉了，Excel 路径的起始下标却会把表头行及其上方的行当成
 * 数据行发出去。与其让两条路径各自"随便处理一下"，不如在入口处一律拒绝。
 */
export function assertValidParseOptions(options: SourceParseOptions): void {
  if (options.headerRow !== undefined && options.headerRow < 1) {
    throw new Error(`headerRow 必须从 1 开始（跟 Excel 行号一致），收到 ${options.headerRow}`)
  }
  const headerRow = headerRowOf(options)
  if (options.firstDataRow !== undefined && options.firstDataRow <= headerRow) {
    throw new Error(
      `firstDataRow（${options.firstDataRow}）必须大于 headerRow（${headerRow}）：` +
        '表头行本身不是数据行，表头之上的行也不是。',
    )
  }
  if (typeof options.sheet === 'number' && options.sheet < 0) {
    throw new Error(`sheet 序号不能是负数（0 表示第一张表），收到 ${options.sheet}`)
  }
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

/**
 * 真正有单元格的范围，从 A1 起算；一个单元格都没有时返回 null。
 *
 * 不用工作表自己声明的 `!ref`：手工编辑过的 Excel 里它常常画得比实际数据宽。
 * 真实的 MUJI `CN_001_SKU_MASTER_121.xls` 的 `!ref` 是 `A1:DJ908`（114 列），
 * 而第 114 列一个单元格都没有；后端 xlrd 的 `ncols` 是按实际存在的单元格记录
 * 算的，给出 113。两边列数对不上，跑批前的逐列对账会直接判 400。
 *
 * 起点固定在 A1 而不是 `!ref` 的起点：xlrd 的行列下标也从 0 起算，数据从 C 列
 * 开始的表，两边要对得上就不能把 C 列当成第 0 列。
 */
function actualCellBounds(
  XLSX: typeof import('xlsx'),
  sheet: import('xlsx').WorkSheet,
): import('xlsx').Range | null {
  let maxRow = -1
  let maxCol = -1
  for (const address of Object.keys(sheet)) {
    // '!ref' / '!merges' 这些元数据键不是单元格地址。
    if (address.startsWith('!')) continue
    const { r, c } = XLSX.utils.decode_cell(address)
    if (r > maxRow) maxRow = r
    if (c > maxCol) maxCol = c
  }
  if (maxRow < 0 || maxCol < 0) return null
  return { s: { r: 0, c: 0 }, e: { r: maxRow, c: maxCol } }
}

/**
 * 一张工作表的全部行：满宽、无空洞，宽度按实际有单元格的列算。
 *
 * `defval` 让 SheetJS 给每个空格子发出 `''`：不传的话它会截掉行尾的空格子、
 * 并在行中间留下稀疏空洞，同一张表在这里拿到的行宽会参差不齐。CSV 路径和
 * 后端（xlrd）给出的都是满宽行，不传就是一处前后端的形状分叉。
 *
 * `range` 把范围收窄到实际单元格——只砍"根本没有任何单元格"的尾列尾行，
 * 中间那些空列名的列一列不动（它们有单元格，位置也必须保住）。
 *
 * 光靠 `sheetRows` 是不够的：xlsx 的读取器在限行时会顺带按实际单元格重算
 * `!ref`，BIFF8（.xls）的读取器不会——实测同一份内容写成两种格式，
 * `sheetRows: 1` 时 xlsx 给 `A1:C1`、biff8 给 `A1:E1`。真实的 MUJI 表正是 .xls。
 */
function sheetRowsOf(
  XLSX: typeof import('xlsx'),
  sheet: import('xlsx').WorkSheet,
): unknown[][] {
  const range = actualCellBounds(XLSX, sheet)
  if (range === null) return []
  return XLSX.utils.sheet_to_json<unknown[]>(sheet, { header: 1, defval: '', range })
}

/**
 * 日期格子按**本地**年月日格式化，不用 toISOString()。
 *
 * SheetJS 在 cellDates: true 下发出的是本地时区的 Date，而 toISOString() 会先
 * 折算成 UTC：东八区下 `2026-01-15 00:00` 会变成 `2026-01-14T16:00:00Z`，截出
 * 来的日期整整差一天。后端 convert_excel_cell_to_string 用的是 strftime，
 * 拿到的是 `2026-01-15`——同一个格子两端得到不同的字符串，而这正是这条管线
 * 反复出问题的那一类分叉：日期型的表头格子会让跑批前的逐列对账直接判 400，
 * 引导建模里的日期样例值则会整体早一天显示。
 *
 * 带时分秒的格子这里仍然只给日期（后端给的是 `%Y-%m-%d %H:%M:%S`），那一半
 * 分叉牵动列类型推断，另开任务处理。
 */
function dateToLocalDateString(value: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}`
}

function cellToString(cell: unknown): string {
  if (cell === undefined || cell === null) return ''
  if (cell instanceof Date) return dateToLocalDateString(cell)
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
  assertNotTooLargeForExcel(file)
  const XLSX = await loadXlsx()
  const buffer = await file.arrayBuffer()
  // sheetRows: 1——只要表名，不用把每张表的内容都解析出来。
  // cellDates 跟 readExcelRows / readExcelHeader 保持一致：三条路径读同一个
  // 单元格必须得到同一个字符串。
  const workbook = XLSX.read(buffer, { type: 'array', cellDates: true, sheetRows: 1 })
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
  assertValidParseOptions(options)
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
      onHeader(headerFrom(parseDelimitedHeaderLine(line, delimiter)))
      continue
    }
    if (lineNumber < firstDataRow) continue
    // 跳过完全空白的行（比如文件末尾的换行符），但不跳过"看起来空但有
    // 分隔符"的行——那是真实的空值行，短行补齐由调用方处理。
    if (line === '') continue
    onRow(parseDelimitedHeaderLine(line, delimiter))
  }
}

/**
 * 四条 Excel 路径（逐行读、读表头、读预览、列工作表名）共用的体积闸门。
 *
 * 每一条都要 `file.arrayBuffer()` 把整个工作簿读进内存——sheetRows 只是让
 * SheetJS 少解析几行，读盘量一点没少。少一条检查就少一条防线，而超限的
 * 表现是浏览器直接卡死，不是报错。
 */
function assertNotTooLargeForExcel(file: File): void {
  if (file.size > MAX_XLSX_BYTES) {
    throw new Error(
      `xlsx 文件过大（${file.size} 字节，上限 ${MAX_XLSX_BYTES} 字节）：xlsx 是二进制容器格式，` +
        '必须整个读进内存才能解析，文件太大会让浏览器卡死。请换一个更小的文件，或导出为 CSV。',
    )
  }
}

async function readExcelRows(
  file: File,
  options: SourceParseOptions,
  onHeader: (columns: string[]) => void,
  onRow: (row: string[]) => void,
): Promise<void> {
  assertNotTooLargeForExcel(file)
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
  const rows = sheetRowsOf(XLSX, sheet)
  const headerRow = headerRowOf(options)
  const header = rows[headerRow - 1]
  if (header === undefined) return
  onHeader(headerFrom(header.map((cell) => cellToString(cell).trim())))
  for (let index = firstDataRowOf(options) - 1; index < rows.length; index++) {
    onRow((rows[index] ?? []).map(cellToString))
  }
}

/** 读一张表的列名。表头行号超出文件长度时返回空数组。 */
export async function readSourceHeader(
  file: File,
  options: SourceParseOptions = {},
): Promise<string[]> {
  assertValidParseOptions(options)
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
      return headerFrom(parseDelimitedHeaderLine(line, delimiter))
    }
  }
  return []
}

async function readExcelHeader(file: File, options: SourceParseOptions): Promise<string[]> {
  assertNotTooLargeForExcel(file)
  const XLSX = await loadXlsx()
  const buffer = await file.arrayBuffer()
  const headerRow = headerRowOf(options)
  // sheetRows 限制只解析到表头那一行，不用把整个工作簿解析出来。
  // cellDates 跟 readExcelRows 保持一致：日期型的表头单元格不传这个选项会
  // 读成 Excel 内部的浮点序列号（45678），传了才是 Date，两条路径否则会对
  // 同一列给出不同的列名。
  const workbook = XLSX.read(buffer, { type: 'array', cellDates: true, sheetRows: headerRow })
  const sheetName = selectSheetName(workbook, options.sheet)
  if (sheetName === null) {
    if (options.sheet === undefined) return []
    throw sheetNotFoundError(options.sheet, workbook.SheetNames)
  }
  const sheet = workbook.Sheets[sheetName]
  const rows = sheetRowsOf(XLSX, sheet)
  const header = rows[headerRow - 1]
  if (header === undefined) return []
  return headerFrom(header.map((cell) => cellToString(cell).trim()))
}

/**
 * 原始的前若干行，**不受 headerRow / firstDataRow 影响**——探测和预览要看的
 * 就是文件原始的形状，包括表头之上的标题带。不做重名去重，因为这里给的不是
 * 列名。options 里只有 `sheet` 起作用：预览的是哪张表得听用户的，读哪几行
 * 不听。
 */
export async function readSourcePreview(
  file: File,
  rowCount: number,
  options: SourceParseOptions = {},
): Promise<string[][]> {
  assertValidParseOptions(options)
  if (rowCount <= 0) return []
  if (isExcel(file)) return readExcelPreview(file, rowCount, options)
  const delimiter = delimiterOf(file)
  const rows: string[][] = []
  for await (const line of readDelimitedLines(file)) {
    rows.push(parseDelimitedHeaderLine(line, delimiter))
    if (rows.length >= rowCount) break
  }
  return rows
}

async function readExcelPreview(
  file: File,
  rowCount: number,
  options: SourceParseOptions,
): Promise<string[][]> {
  assertNotTooLargeForExcel(file)
  const XLSX = await loadXlsx()
  const buffer = await file.arrayBuffer()
  const workbook = XLSX.read(buffer, { type: 'array', cellDates: true, sheetRows: rowCount })
  const sheetName = selectSheetName(workbook, options.sheet)
  if (sheetName === null) {
    if (options.sheet === undefined) return []
    throw sheetNotFoundError(options.sheet, workbook.SheetNames)
  }
  const sheet = workbook.Sheets[sheetName]
  const rows = sheetRowsOf(XLSX, sheet)
  return rows.slice(0, rowCount).map((row) => (row ?? []).map((cell) => cellToString(cell).trim()))
}
