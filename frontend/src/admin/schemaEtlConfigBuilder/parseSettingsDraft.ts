import { assertValidParseOptions, type SourceParseOptions } from './sourceParser'

/**
 * 解析设置输入框里的**原样文字**。
 *
 * 不直接用 SourceParseOptions 存界面状态，是因为输入过程中会出现它表达不了
 * 的中间态：清空输入框的那一瞬间值是空字符串，把 10 改成 2 的中途会出现 0。
 * 这些值喂给解析器会被 assertValidParseOptions 拒绝（拒绝是对的，规则跟后端
 * 逐条对齐），所以中间态停在这一层，翻译不过去就不往下走。
 */
export interface ParseDraft {
  sheet: string | number | undefined
  headerRow: string
  firstDataRow: string
}

export type DraftResult =
  | { ok: true; options: SourceParseOptions }
  | { ok: false; message: string }

export function draftFromOptions(options: SourceParseOptions): ParseDraft {
  return {
    sheet: options.sheet,
    headerRow: String(options.headerRow ?? 1),
    firstDataRow: options.firstDataRow === undefined ? '' : String(options.firstDataRow),
  }
}

function parseRowNumber(text: string): number | null {
  return /^\d+$/.test(text) ? Number(text) : null
}

/**
 * 把输入框里的文字翻成解析选项；翻不过去时返回一句给用户看的话。
 *
 * 校验不在这里重写一遍：调 assertValidParseOptions，把它抛出来的说法原样
 * 交给界面显示。捕获在这里不是"当作没发生"——调用方拿到 ok:false 就停在
 * 上一次的列名并把这句话显示出来，用户看得见自己填的东西没有生效。
 */
export function optionsFromDraft(draft: ParseDraft): DraftResult {
  const headerRowText = draft.headerRow.trim()
  if (headerRowText === '') {
    return { ok: false, message: '表头行不能留空：不填的话不知道该从哪一行取列名。' }
  }
  const headerRow = parseRowNumber(headerRowText)
  if (headerRow === null) {
    return { ok: false, message: `表头行要填一个行号（整数），收到 ${JSON.stringify(headerRowText)}。` }
  }
  const firstDataRowText = draft.firstDataRow.trim()
  let firstDataRow: number | undefined
  if (firstDataRowText !== '') {
    const parsed = parseRowNumber(firstDataRowText)
    if (parsed === null) {
      return {
        ok: false,
        message: `首数据行要填一个行号（整数）或留空，收到 ${JSON.stringify(firstDataRowText)}。`,
      }
    }
    firstDataRow = parsed
  }
  const options: SourceParseOptions = {}
  if (draft.sheet !== undefined) options.sheet = draft.sheet
  options.headerRow = headerRow
  if (firstDataRow !== undefined) options.firstDataRow = firstDataRow
  try {
    assertValidParseOptions(options)
  } catch (err) {
    return { ok: false, message: err instanceof Error ? err.message : String(err) }
  }
  return { ok: true, options }
}

/**
 * 表头和首数据行之间被跳过的行号区间；紧跟表头时为 null。
 *
 * 界面必须把它说出来：首数据行填大了会安静地少读数据，不显示的话用户看不出
 * 自己丢了几行。
 */
export function skippedRows(options: SourceParseOptions): { from: number; to: number } | null {
  const headerRow = options.headerRow ?? 1
  const firstDataRow = options.firstDataRow ?? headerRow + 1
  if (firstDataRow <= headerRow + 1) return null
  return { from: headerRow + 1, to: firstDataRow - 1 }
}
