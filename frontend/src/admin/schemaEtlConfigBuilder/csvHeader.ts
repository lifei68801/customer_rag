// 只读文件开头一小段就够了——表头只在第一行，不需要把整个文件读进内存。
// 64KB 远超任何现实场景下单行表头的长度（哪怕几百个中文列名也远远不到这个量级）。
const HEADER_READ_BYTES = 65536

export async function readCsvHeaderColumns(file: File): Promise<string[]> {
  const chunk = await file.slice(0, HEADER_READ_BYTES).text()
  const firstLineEnd = chunk.search(/\r\n|\r|\n/)
  const firstLine = firstLineEnd === -1 ? chunk : chunk.slice(0, firstLineEnd)
  return parseCsvHeaderLine(firstLine)
}

// 按标准 CSV 引号规则（RFC 4180）解析一行，跟后端 Python csv 模块的解析规则
// 对齐——如果表头列名里本身带逗号，必须用双引号包裹（如 "A,B"），双引号
// 内部的字面双引号写成两个连续双引号（""）转义，这里同样处理这两种情况。
function parseCsvHeaderLine(line: string): string[] {
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
    } else if (char === ',') {
      columns.push(current)
      current = ''
    } else {
      current += char
    }
  }
  columns.push(current)
  return columns.map((c) => c.trim())
}
