import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'

interface RunSummary {
  run_id: string
  status: string
  started_at: string
  finished_at: string | null
}

interface SkippedRow {
  label: string
  source_file: string
  row_number: number
  reason: string
}

interface EtlRunReport {
  entities_written?: number
  entities_skipped?: number
  relations_written?: number
  relations_skipped?: number
  written_by_type?: Record<string, number>
  skipped_by_type?: Record<string, number>
  skipped_rows?: SkippedRow[]
  skipped_mappings?: { label: string; source_file: string; reason: string }[]
}

interface RunDetail {
  run_id: string
  status: string
  started_at: string
  finished_at: string | null
  report: EtlRunReport | null
  error: string | null
}

const SKIPPED_ROWS_PREVIEW_LIMIT = 50

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

export function SchemaEtlPage() {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const [confirmed, setConfirmed] = useState<boolean | null>(null)
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [selectedRun, setSelectedRun] = useState<RunDetail | null>(null)
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)
  // 轮询期间才需要知道"上一次拿到的历史列表里是不是还有 running 记录"，
  // 不需要触发重渲染。
  const hasRunningRef = useRef(false)
  // 让 handleUpload 能在提交完成后立即"踢"一次轮询循环，不用等已经排好
  // 队的 setTimeout 走完最坏 15 秒才发现新记录出现了。
  const pollNowRef = useRef<() => Promise<void>>(async () => {})

  useEffect(() => {
    document.title = 'ETL 跑批 · 管理后台'
  }, [])

  const refreshStatus = useCallback(async () => {
    if (!sessionToken) return
    const response = await adminFetch(
      `/api/admin/${encodeURIComponent(tenantId)}/schema-etl/status`,
      sessionToken,
    )
    const data = (await response.json()) as { ontology_confirmed: boolean }
    setConfirmed(data.ontology_confirmed)
  }, [sessionToken, tenantId])

  const refreshRuns = useCallback(async () => {
    if (!sessionToken) return
    const response = await adminFetch(
      `/api/admin/${encodeURIComponent(tenantId)}/schema-etl/runs`,
      sessionToken,
    )
    const data = (await response.json()) as { runs: RunSummary[] }
    setRuns(data.runs)
    hasRunningRef.current = data.runs.some((r) => r.status === 'running')
  }, [sessionToken, tenantId])

  useEffect(() => {
    refreshStatus().catch((err) => console.error('查询 schema 确认状态失败', err))
  }, [refreshStatus])

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null

    const poll = async () => {
      if (timer) clearTimeout(timer)
      try {
        await refreshRuns()
      } catch (err) {
        console.error('ETL 跑批列表刷新失败', err)
      }
      if (cancelled) return
      // 没有 running 记录时没必要每 3 秒打一次后端——退避到 15 秒；提交
      // 完成后会通过 pollNowRef 主动跳过这次等待，不是真的要等满 15 秒。
      const interval = hasRunningRef.current ? 3000 : 15000
      timer = setTimeout(poll, interval)
    }
    pollNowRef.current = poll
    poll()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [refreshRuns])

  useEffect(() => {
    if (!selectedRunId || !sessionToken) {
      setSelectedRun(null)
      return
    }
    let cancelled = false
    const load = async () => {
      const response = await adminFetch(
        `/api/admin/${encodeURIComponent(tenantId)}/schema-etl/runs/${encodeURIComponent(selectedRunId)}`,
        sessionToken,
      )
      if (cancelled) return
      const data = (await response.json()) as RunDetail
      setSelectedRun(data)
    }
    load().catch((err) => console.error('跑批详情加载失败', err))
    return () => {
      cancelled = true
    }
  }, [selectedRunId, sessionToken, tenantId])

  const handleUpload = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!sessionToken) return
    const form = event.currentTarget
    const configInput = form.elements.namedItem('config') as HTMLInputElement
    const dataFilesInput = form.elements.namedItem('data_files') as HTMLInputElement
    const configFile = configInput.files?.[0]
    if (!configFile) return

    setUploading(true)
    setUploadError(null)
    try {
      const formData = new FormData()
      formData.append('config', configFile)
      for (const file of Array.from(dataFilesInput.files ?? [])) {
        formData.append('data_files', file)
      }
      const response = await adminFetch(
        `/api/admin/${encodeURIComponent(tenantId)}/schema-etl/runs`,
        sessionToken,
        { method: 'POST', body: formData },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '启动失败'))
      }
      form.reset()
      await pollNowRef.current()
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : '启动失败')
    } finally {
      setUploading(false)
    }
  }

  const downloadReportUrl = selectedRun
    ? `/api/admin/${encodeURIComponent(tenantId)}/schema-etl/runs/${encodeURIComponent(selectedRun.run_id)}/report.csv`
    : null

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-xl font-bold text-ink">ETL 跑批（租户：{tenantId}）</h1>

      {confirmed === false && (
        <div className="border-2 border-ink bg-accent-yellow px-3 py-2 text-sm text-ink shadow-brutal-sm">
          该租户本体 schema 尚未确认，请先完成本体 schema 确认后再触发 ETL。
        </div>
      )}

      <form
        onSubmit={handleUpload}
        aria-disabled={confirmed !== true}
        className="flex flex-col gap-3 border-2 border-ink bg-card p-4 shadow-brutal"
      >
        <label className="flex flex-col gap-1 text-sm font-bold text-ink">
          列映射配置（YAML）
          <input
            type="file"
            name="config"
            accept=".yaml,.yml"
            required
            disabled={confirmed !== true}
            className="text-ink"
          />
        </label>
        <label className="flex flex-col gap-1 text-sm font-bold text-ink">
          数据文件（CSV，可多选）
          <input
            type="file"
            name="data_files"
            accept=".csv"
            multiple
            disabled={confirmed !== true}
            className="text-ink"
          />
        </label>
        {uploadError && (
          <p role="alert" className="text-sm text-ink">
            {uploadError}
          </p>
        )}
        <button
          type="submit"
          disabled={uploading || confirmed !== true}
          className={`min-h-[44px] cursor-pointer self-start border-2 border-ink bg-accent-pink px-5 py-2.5 font-bold text-ink shadow-brutal transition active:translate-x-[2px] active:translate-y-[2px] active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
        >
          {uploading ? '提交中…' : '开始运行'}
        </button>
      </form>

      <div className="flex flex-col gap-2">
        <h2 className="font-bold text-ink">历史跑批</h2>
        {runs.length === 0 && <p className="text-ink-soft">还没有任何跑批记录。</p>}
        {runs.length > 0 && (
          <div className="overflow-x-auto border-2 border-ink bg-card shadow-brutal-sm">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b-2 border-ink bg-paper text-ink">
                  <th className="px-3 py-2">run_id</th>
                  <th className="px-3 py-2">状态</th>
                  <th className="px-3 py-2">开始时间</th>
                  <th className="px-3 py-2">结束时间</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr
                    key={run.run_id}
                    onClick={() => setSelectedRunId(run.run_id)}
                    className={`cursor-pointer border-b border-ink/20 text-ink last:border-b-0 hover:bg-paper ${
                      selectedRunId === run.run_id ? 'bg-paper' : ''
                    }`}
                  >
                    <td className="px-3 py-2 font-mono text-xs">{run.run_id}</td>
                    <td className="px-3 py-2">{run.status}</td>
                    <td className="px-3 py-2">{run.started_at}</td>
                    <td className="px-3 py-2">{run.finished_at ?? '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {selectedRun && (
        <div className="flex flex-col gap-3 border-2 border-ink bg-card p-4 shadow-brutal">
          <h2 className="font-bold text-ink">跑批详情：{selectedRun.run_id}</h2>
          {selectedRun.status === 'failed' && (
            <p role="alert" className="border-2 border-status-error bg-card px-3 py-2 text-sm text-ink shadow-brutal-sm">
              失败：{selectedRun.error}
            </p>
          )}
          {selectedRun.report && (
            <>
              <p className="text-sm text-ink">
                实体写入 {selectedRun.report.entities_written ?? 0} 条，跳过{' '}
                {selectedRun.report.entities_skipped ?? 0} 条；关系写入{' '}
                {selectedRun.report.relations_written ?? 0} 条，跳过{' '}
                {selectedRun.report.relations_skipped ?? 0} 条
              </p>
              <div className="overflow-x-auto border-2 border-ink shadow-brutal-sm">
                <table className="w-full text-left text-sm">
                  <thead>
                    <tr className="border-b-2 border-ink bg-paper text-ink">
                      <th className="px-3 py-2">类型</th>
                      <th className="px-3 py-2">写入</th>
                      <th className="px-3 py-2">跳过</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Array.from(
                      new Set([
                        ...Object.keys(selectedRun.report.written_by_type ?? {}),
                        ...Object.keys(selectedRun.report.skipped_by_type ?? {}),
                      ]),
                    ).map((label) => (
                      <tr key={label} className="border-b border-ink/20 text-ink last:border-b-0">
                        <td className="px-3 py-2">{label}</td>
                        <td className="px-3 py-2">
                          {selectedRun.report?.written_by_type?.[label] ?? 0}
                        </td>
                        <td className="px-3 py-2">
                          {selectedRun.report?.skipped_by_type?.[label] ?? 0}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {selectedRun.report.skipped_rows && selectedRun.report.skipped_rows.length > 0 && (
                <div className="flex flex-col gap-2">
                  <h3 className="font-bold text-ink">
                    跳过明细（预览前 {SKIPPED_ROWS_PREVIEW_LIMIT} 条，共{' '}
                    {selectedRun.report.skipped_rows.length} 条）
                  </h3>
                  <div className="overflow-x-auto border-2 border-ink shadow-brutal-sm">
                    <table className="w-full text-left text-sm">
                      <thead>
                        <tr className="border-b-2 border-ink bg-paper text-ink">
                          <th className="px-3 py-2">文件</th>
                          <th className="px-3 py-2">行号</th>
                          <th className="px-3 py-2">原因</th>
                        </tr>
                      </thead>
                      <tbody>
                        {selectedRun.report.skipped_rows
                          .slice(0, SKIPPED_ROWS_PREVIEW_LIMIT)
                          .map((row, idx) => (
                            <tr key={idx} className="border-b border-ink/20 text-ink last:border-b-0">
                              <td className="px-3 py-2">{row.source_file}</td>
                              <td className="px-3 py-2">{row.row_number}</td>
                              <td className="px-3 py-2">{row.reason}</td>
                            </tr>
                          ))}
                      </tbody>
                    </table>
                  </div>
                  {downloadReportUrl && (
                    <a
                      href={downloadReportUrl}
                      className={`self-start text-sm font-bold text-ink underline decoration-2 underline-offset-2 ${focusRing}`}
                    >
                      下载完整报告 CSV
                    </a>
                  )}
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}
