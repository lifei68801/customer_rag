import { useState, type ChangeEvent } from 'react'
import { assignRoles } from '../../guidedOntology/columnRoles'
import { scanTableFile } from '../../guidedOntology/columnStats'
import { draftFromOptions, optionsFromDraft, type ParseDraft } from '../../schemaEtlConfigBuilder/parseSettingsDraft'
import { listSheetNames } from '../../schemaEtlConfigBuilder/sourceParser'
import { alignTable, mergeAlignments, type ScannedTable } from '../alignToSkeleton'
import { parseOptionsOf } from '../types'
import type { WorkspaceState } from '../types'
import { panelClass, primaryButtonClass, secondaryButtonClass, tagClass } from '../ui'

/**
 * 数据发现：传表 → 扫列 → 按别名对齐 → 把结果合并进工作区。
 *
 * 解析选项（工作表、表头行）必须在这里就能填：MUJI 那张表表头在第 6 行，
 * 按缺省第 1 行读出来的列名全是空的，对齐一条都命不中，而界面上看不出原因。
 */
export function DataPanel(props: {
  state: WorkspaceState
  busy: boolean
  onMerged: (next: WorkspaceState) => void
  onPromote: (file: string, column: string) => void
}) {
  const [file, setFile] = useState<File | null>(null)
  const [sheetNames, setSheetNames] = useState<string[]>([])
  const [draft, setDraft] = useState<ParseDraft>(draftFromOptions({}))
  const [error, setError] = useState<string | null>(null)
  const [scanning, setScanning] = useState(false)

  const handleFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const picked = event.target.files?.[0]
    if (!picked) return
    setError(null)
    setFile(picked)
    setDraft(draftFromOptions(parseOptionsOf(props.state.sources.find((s) => s.file === picked.name))))
    try {
      setSheetNames(await listSheetNames(picked))
    } catch {
      // CSV 没有工作表概念，listSheetNames 抛错是正常的——不显示工作表选择即可
      setSheetNames([])
    }
  }

  const handleScan = async () => {
    if (!file) return
    const parsed = optionsFromDraft(draft)
    if (!parsed.ok) {
      setError(parsed.message)
      return
    }
    setScanning(true)
    setError(null)
    try {
      const stats = await scanTableFile(file, parsed.options)
      const table: ScannedTable = { file: file.name, roled: assignRoles(stats) }
      const withSource: WorkspaceState = {
        ...props.state,
        sources: [
          ...props.state.sources.filter((s) => s.file !== file.name),
          {
            file: file.name,
            sheet: parsed.options.sheet ?? null,
            header_row: parsed.options.headerRow,
            first_data_row: parsed.options.firstDataRow,
          },
        ],
      }
      props.onMerged(
        mergeAlignments(withSource, [alignTable(table, withSource.term_types)], [table]),
      )
    } catch (err) {
      // 扫描失败必须说清原因（比如 xlsx 超过体积上限），不能静静停住
      setError(err instanceof Error ? err.message : '扫描失败')
    } finally {
      setScanning(false)
    }
  }

  const matched = props.state.term_types.filter((t) => t.data_match !== null)
  const unmatchedEntries = Object.entries(props.state.unmatched_columns)

  return (
    <div className="flex flex-col gap-4">
      <section className={`${panelClass} flex flex-col gap-3`}>
        <h2 className="font-mono text-base font-semibold text-ink">上传数据表</h2>
        <input
          type="file"
          accept=".csv,.tsv,.txt,.xlsx,.xls"
          aria-label="选择数据表"
          onChange={handleFile}
          className="text-sm text-ink"
        />
        {file && (
          <div className="flex flex-wrap items-end gap-3">
            {sheetNames.length > 0 && (
              <label className="flex flex-col gap-1 text-sm text-ink">
                工作表
                <select
                  className="rounded-control border border-subtle bg-paper px-2 py-1"
                  value={String(draft.sheet ?? '')}
                  onChange={(e) =>
                    setDraft({ ...draft, sheet: e.target.value === '' ? undefined : e.target.value })
                  }
                >
                  <option value="">第一个</option>
                  {sheetNames.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <label className="flex flex-col gap-1 text-sm text-ink">
              表头行
              <input
                className="w-24 rounded-control border border-subtle bg-paper px-2 py-1"
                value={draft.headerRow}
                onChange={(e) => setDraft({ ...draft, headerRow: e.target.value })}
              />
            </label>
            <label className="flex flex-col gap-1 text-sm text-ink">
              首数据行
              <input
                className="w-24 rounded-control border border-subtle bg-paper px-2 py-1"
                placeholder="紧跟表头"
                value={draft.firstDataRow}
                onChange={(e) => setDraft({ ...draft, firstDataRow: e.target.value })}
              />
            </label>
            <button
              type="button"
              className={primaryButtonClass}
              disabled={scanning || props.busy}
              onClick={handleScan}
            >
              {scanning ? '扫描中…' : '扫描并对齐'}
            </button>
          </div>
        )}
        {error && (
          <p role="alert" className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink">
            {error}
          </p>
        )}
      </section>

      <section className={`${panelClass} flex flex-col gap-2`}>
        <h2 className="font-mono text-base font-semibold text-ink">对上的概念</h2>
        {matched.length === 0 && <p className="text-sm text-ink-soft">还没有概念对上数据。</p>}
        {matched.map((term) => (
          <p key={term.value} className="text-sm text-ink">
            <span className="font-mono font-semibold">{term.value}</span>
            <span className={`ml-2 ${tagClass}`}>
              {`${term.data_match!.source_file} · 键列 ${term.data_match!.key_columns.join('/')} · ${term.data_match!.matched_by}`}
            </span>
          </p>
        ))}
      </section>

      <section className={`${panelClass} flex flex-col gap-2`}>
        <h2 className="font-mono text-base font-semibold text-ink">数据里还有这些，骨架里没有</h2>
        {unmatchedEntries.length === 0 && <p className="text-sm text-ink-soft">没有剩下的列。</p>}
        {unmatchedEntries.map(([fileName, columns]) => (
          <div key={fileName} className="flex flex-col gap-1">
            <p className="text-sm font-bold text-ink">{fileName}</p>
            <div className="flex flex-wrap gap-2">
              {columns.map((column) => (
                <button
                  key={column}
                  type="button"
                  className={secondaryButtonClass}
                  disabled={props.busy}
                  onClick={() => props.onPromote(fileName, column)}
                >
                  {`把 ${column} 提升为实体类型`}
                </button>
              ))}
            </div>
          </div>
        ))}
      </section>
    </div>
  )
}
