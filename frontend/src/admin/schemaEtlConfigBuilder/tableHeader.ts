import { readSourceHeader } from './sourceParser'
import type { SourceParseOptions } from './sourceParser'

/**
 * 读一张表的列名。解析实现在 sourceParser.ts——这个文件曾经自己实现了一份，
 * columnStats.ts 又实现了另一份，两份都写死"第一个 sheet、第一行表头"，且
 * 规则会悄悄分叉（2026-09-15 的 9c71cf9 让它们分叉过一次）。现在只剩一份。
 */
export async function readTableHeaderColumns(
  file: File,
  options?: SourceParseOptions,
): Promise<string[]> {
  return readSourceHeader(file, options)
}

export { parseDelimitedHeaderLine } from './sourceParser'
