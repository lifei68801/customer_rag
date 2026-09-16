import type { EtlMappingSummary, StoredSourceParseOptions } from '../etlMappingApi'
import type { SourceParseOptions } from './sourceParser'

/**
 * 把存着的解析选项翻译回编辑器用的形状。
 *
 * 存了不回填的话，用户重开页面看到的是"表头在第 1 行"——一份他没配过的
 * 设置，而且没有任何提示说设置被换掉了。他多半会以为配置丢了，重配一遍。
 *
 * 后端用 snake_case、null 表示"没设"；编辑器用 camelCase、undefined 表示
 * "没设"。翻译集中在这一处，不散到组件里。
 */
export function storedParseOptionsFor(
  summary: EtlMappingSummary | null | undefined,
  fileName: string,
): SourceParseOptions | null {
  const stored: StoredSourceParseOptions | undefined = summary?.sources?.find(
    (s) => s.file === fileName,
  )
  if (!stored) return null
  const options: SourceParseOptions = {}
  if (stored.sheet !== null) options.sheet = stored.sheet
  if (stored.header_row !== 1) options.headerRow = stored.header_row
  if (stored.first_data_row !== null) options.firstDataRow = stored.first_data_row
  return options
}
