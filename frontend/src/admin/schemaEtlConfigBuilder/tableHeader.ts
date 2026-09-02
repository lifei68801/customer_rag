// 只读文件开头一小段就够了——表头只在第一行，不需要把整个文件读进内存。
// 64KB 远超任何现实场景下单行表头的长度（哪怕几百个中文列名也远远不到这个量级）。
const HEADER_READ_BYTES = 65536

// 按扩展名分流：CSV/TSV 是纯文本，只读文件开头一小段当文本解析；XLSX/XLS
// 是二进制 zip 容器格式，slice().text() 这种读法完全不适用，必须交给
// SheetJS 解析（file.arrayBuffer() 仍要读入全部字节——二进制容器格式没法
// 只读开头一段，但用 sheetRows: 1 限制只解析表头所在的第一行，不用把
// 整个工作簿解析出来）。固定读第一个工作表，其余 sheet 忽略——见
// docs/superpowers/specs/2026-08-21-schema-etl-multi-format-upload.md 决策 2。
export async function readTableHeaderColumns(file: File): Promise<string[]> {
  const extension = file.name.slice(file.name.lastIndexOf('.')).toLowerCase()
  if (extension === '.xlsx' || extension === '.xls') {
    return readExcelHeaderColumns(file)
  }
  const delimiter = extension === '.tsv' ? '\t' : ','
  return readDelimitedHeaderColumns(file, delimiter)
}

async function readDelimitedHeaderColumns(file: File, delimiter: string): Promise<string[]> {
  const chunk = await file.slice(0, HEADER_READ_BYTES).text()
  const firstLineEnd = chunk.search(/\r\n|\r|\n/)
  const firstLine = firstLineEnd === -1 ? chunk : chunk.slice(0, firstLineEnd)
  return parseDelimitedHeaderLine(firstLine, delimiter)
}

// 按标准 CSV 引号规则（RFC 4180）解析一行，跟后端 Python csv 模块的解析规则
// 对齐——如果表头列名里本身带分隔符，必须用双引号包裹（如 "A,B"），双引号
// 内部的字面双引号写成两个连续双引号（""）转义，这里同样处理这两种情况。
// TSV 复用同一套引号规则，只是把逗号换成传入的 delimiter。
//
// 导出给 guidedOntology/columnStats.ts 复用：那边扫描整份文件的数据行，
// 需要跟这里表头解析完全一致的引号规则，不能另写一份、让两处标准悄悄
// 分叉。
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

async function readExcelHeaderColumns(file: File): Promise<string[]> {
  // 动态导入而不是顶层 import：SheetJS 压缩后接近 500KB，前台聊天页和
  // 后台管理页共享同一份打包产物（App.tsx 没有对路由做代码分割），静态
  // import 会让只访问聊天页的普通用户也下载这个库。动态 import 让 Vite
  // 把它拆成独立 chunk，只有真正调用到这个函数（后台上传 Excel 时）才
  // 会触发下载。
  const XLSX = await import('xlsx')
  const buffer = await file.arrayBuffer()
  const workbook = XLSX.read(buffer, { type: 'array', sheetRows: 1 })
  const firstSheetName = workbook.SheetNames[0]
  if (!firstSheetName) return []
  const sheet = workbook.Sheets[firstSheetName]
  const rows = XLSX.utils.sheet_to_json<unknown[]>(sheet, { header: 1 })
  const headerRow = rows[0] ?? []
  return headerRow.map((cell) => (cell === undefined || cell === null ? '' : String(cell).trim()))
}
