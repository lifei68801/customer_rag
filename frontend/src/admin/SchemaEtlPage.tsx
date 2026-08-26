import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { adminFetch, extractErrorDetail } from './adminApi'
import { SchemaEtlConfigBuilder } from './schemaEtlConfigBuilder/SchemaEtlConfigBuilder'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'
import { useToast } from './ToastContext'
import { CopyButton } from './CopyButton'
import { TaskStatusBadge } from './TaskStatusBadge'

// etl_runs 表的 status 只有这三种取值（app/graphrag/etl_runs_store.py），
// 映射成统一的徽章语气 + 中文文案。
function etlRunStatusBadge(status: string): { tone: 'active' | 'success' | 'error' | 'neutral'; label: string } {
  if (status === 'running') return { tone: 'active', label: '运行中' }
  if (status === 'completed') return { tone: 'success', label: '已完成' }
  if (status === 'failed') return { tone: 'error', label: '失败' }
  return { tone: 'neutral', label: status }
}

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

interface SampleFile {
  filename: string
  content: string
}

const SKIPPED_ROWS_PREVIEW_LIMIT = 50

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

export function SchemaEtlPage() {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const showToast = useToast()
  const [confirmed, setConfirmed] = useState<boolean | null>(null)
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [selectedRun, setSelectedRun] = useState<RunDetail | null>(null)
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [downloadError, setDownloadError] = useState<string | null>(null)
  const [downloadingReport, setDownloadingReport] = useState(false)
  const [sampleFiles, setSampleFiles] = useState<SampleFile[] | null>(null)
  const [sampleSelectedFilename, setSampleSelectedFilename] = useState<string | null>(null)
  const [sampleLoading, setSampleLoading] = useState(false)
  const [sampleError, setSampleError] = useState<string | null>(null)
  const [downloadingSample, setDownloadingSample] = useState(false)
  // 向导是面向新手的主路径，默认展开——不能让用户先自己发现"这里能点开"
  // 才看到引导流程；裸上传表单是面向已有 config.yaml 的老手场景，默认折叠
  // 在下面的"高级"区块里，见 uploadFormExpanded。
  const [builderExpanded, setBuilderExpanded] = useState(true)
  const [uploadFormExpanded, setUploadFormExpanded] = useState(false)
  // 轮询期间才需要知道"上一次拿到的历史列表里是不是还有 running 记录"，
  // 不需要触发重渲染。
  const hasRunningRef = useRef(false)
  // 让 handleUpload 能在提交完成后立即"踢"一次轮询循环，不用等已经排好
  // 队的 setTimeout 走完最坏 15 秒才发现新记录出现了。
  const pollNowRef = useRef<() => Promise<void>>(async () => {})

  useEffect(() => {
    document.title = '表格导入 · 管理后台'
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

  // 切换租户时，之前缓存的示例文件属于旧租户，必须清空，否则下次展开会
  // 直接复用过期数据（sampleFiles !== null 会跳过重新请求）。
  useEffect(() => {
    setSampleFiles(null)
    setSampleSelectedFilename(null)
    setSampleError(null)
  }, [tenantId])

  useEffect(() => {
    if (!uploadFormExpanded || sampleFiles !== null || sampleLoading || !sessionToken) return
    let cancelled = false
    const load = async () => {
      setSampleLoading(true)
      setSampleError(null)
      try {
        const response = await adminFetch(
          `/api/admin/${encodeURIComponent(tenantId)}/schema-etl/sample`,
          sessionToken,
        )
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(extractErrorDetail(body, '生成示例失败'))
        }
        const data = (await response.json()) as { files: SampleFile[] }
        if (cancelled) return
        setSampleFiles(data.files)
        setSampleSelectedFilename(data.files[0]?.filename ?? null)
      } catch (err) {
        if (!cancelled) setSampleError(err instanceof Error ? err.message : '生成示例失败')
      } finally {
        if (!cancelled) setSampleLoading(false)
      }
    }
    load()
    return () => {
      cancelled = true
    }
    // sampleLoading 故意不放进依赖数组：它是这个 effect 自己在 load() 里第一步
    // set 的状态，放进来会导致 setSampleLoading(true) 触发 effect 自我重跑——
    // 重跑时 cleanup 先把上一次的 cancelled 置 true，新一轮的 guard 又因为
    // sampleLoading 已经是 true 而直接 return，原来那次真正在飞的请求最终拿到
    // 结果时发现自己已被标记 cancelled，直接丢弃，sampleLoading 永远卡在 true。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [uploadFormExpanded, sampleFiles, sessionToken, tenantId])

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
      showToast('已提交运行')
      form.reset()
      await pollNowRef.current()
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : '启动失败')
    } finally {
      setUploading(false)
    }
  }

  const handleDownloadReport = async () => {
    if (!sessionToken || !selectedRun || downloadingReport) return
    setDownloadError(null)
    setDownloadingReport(true)
    try {
      const response = await adminFetch(
        `/api/admin/${encodeURIComponent(tenantId)}/schema-etl/runs/${encodeURIComponent(selectedRun.run_id)}/report.csv`,
        sessionToken,
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '下载报告失败'))
      }
      const blob = await response.blob()
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `${selectedRun.run_id}_skipped_rows.csv`
      document.body.appendChild(link)
      link.click()
      link.remove()
      // 触发下载的临时 <a> 元素在 click() 后立即 remove 是安全的（浏览器
      // 已经据此发起下载），但 blob URL 本身要留一段时间——太快 revoke
      // 有些浏览器的下载尚未真正读完数据。跟 DocumentsPage.tsx 的查看
      // 场景用同样的 60 秒延迟释放。
      setTimeout(() => URL.revokeObjectURL(url), 60_000)
    } catch (err) {
      setDownloadError(err instanceof Error ? err.message : '下载报告失败')
    } finally {
      setDownloadingReport(false)
    }
  }

  const handleDownloadSample = async () => {
    if (!sessionToken || downloadingSample) return
    setSampleError(null)
    setDownloadingSample(true)
    try {
      const response = await adminFetch(
        `/api/admin/${encodeURIComponent(tenantId)}/schema-etl/sample.zip`,
        sessionToken,
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '下载示例失败'))
      }
      const blob = await response.blob()
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `${tenantId}_schema_etl_sample.zip`
      document.body.appendChild(link)
      link.click()
      link.remove()
      setTimeout(() => URL.revokeObjectURL(url), 60_000)
    } catch (err) {
      setSampleError(err instanceof Error ? err.message : '下载示例失败')
    } finally {
      setDownloadingSample(false)
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-xl font-bold text-ink">表格导入（租户：{tenantId}）</h1>

      {confirmed === false && (
        <div className="rounded-card border border-subtle bg-accent-secondary px-3 py-2 text-sm text-on-accent">
          该租户本体 schema 尚未确认，请先完成本体 schema 确认后再触发 ETL。
        </div>
      )}

      <div className="flex flex-col gap-2 rounded-panel border border-subtle bg-card">
        <button
          type="button"
          onClick={() => setBuilderExpanded((prev) => !prev)}
          className={`flex items-center justify-between px-4 py-3 text-left font-bold text-ink ${focusRing}`}
        >
          <span>
            配置构建向导
            <span className="ml-2 font-normal text-ink-soft">
              对着自己的数据列一步步配出 config.yaml，不用手写 YAML
            </span>
          </span>
          <span
            aria-hidden="true"
            className={`inline-block transition-transform duration-200 ${builderExpanded ? 'rotate-0' : '-rotate-90'}`}
          >
            ▾
          </span>
        </button>
        {builderExpanded && sessionToken && (
          <SchemaEtlConfigBuilder
            tenantId={tenantId}
            sessionToken={sessionToken}
            disabled={confirmed !== true}
            onSubmitted={() => {
              pollNowRef.current()
            }}
          />
        )}
      </div>

      <div className="flex flex-col gap-2 rounded-panel border border-subtle bg-card">
        <button
          type="button"
          onClick={() => setUploadFormExpanded((prev) => !prev)}
          className={`flex items-center justify-between px-4 py-3 text-left font-bold text-ink ${focusRing}`}
        >
          <span>
            高级：查看示例数据 / 直接上传已有的 config.yaml
            <span className="ml-2 font-normal text-ink-soft">想看看格式范例，或者已经有验证过的配置文件？</span>
          </span>
          <span
            aria-hidden="true"
            className={`inline-block transition-transform duration-200 ${uploadFormExpanded ? 'rotate-0' : '-rotate-90'}`}
          >
            ▾
          </span>
        </button>
        {uploadFormExpanded && (
          <div className="flex flex-col gap-4 border-t border-subtle p-4">
            <div className="flex flex-col gap-3">
              <span className="font-bold text-ink">
                查看示例数据
                <span className="ml-2 font-normal text-ink-soft">
                  基于已确认本体生成一份可以直接跑通的示例，帮助理解 config.yaml 的格式
                </span>
              </span>
              {sampleLoading && <p className="text-ink-soft">生成中…</p>}
              {sampleError && (
                <p role="alert" className="text-sm text-ink">
                  {sampleError}
                </p>
              )}
              {sampleFiles && sampleFiles.length > 0 && (
                <>
                  <div className="flex flex-wrap gap-2">
                    {sampleFiles.map((file) => (
                      <button
                        key={file.filename}
                        type="button"
                        onClick={() => setSampleSelectedFilename(file.filename)}
                        className={`rounded-control border border-subtle px-3 py-1.5 text-xs font-bold ${
                          sampleSelectedFilename === file.filename
                            ? 'bg-accent-primary text-on-accent'
                            : 'bg-paper text-ink'
                        } ${focusRing}`}
                      >
                        {file.filename}
                      </button>
                    ))}
                  </div>
                  <pre className="max-h-80 overflow-auto rounded-card border border-subtle bg-paper p-3 text-xs text-ink">
                    {sampleFiles.find((f) => f.filename === sampleSelectedFilename)?.content ?? ''}
                  </pre>
                  <CopyButton
                    getText={() =>
                      sampleFiles.find((f) => f.filename === sampleSelectedFilename)?.content ?? ''
                    }
                  />
                  <button
                    type="button"
                    onClick={handleDownloadSample}
                    disabled={downloadingSample}
                    className={`self-start rounded-control border border-subtle bg-paper px-4 py-2 text-sm font-bold text-ink transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                  >
                    {downloadingSample ? '下载中…' : '下载全部（zip）'}
                  </button>
                </>
              )}
            </div>

            <form
              onSubmit={handleUpload}
              className="flex flex-col gap-3 border-t border-subtle pt-4"
            >
              <span className="font-bold text-ink">直接上传</span>
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
                数据文件（CSV/TSV/XLSX/XLS，可多选）
                <input
                  type="file"
                  name="data_files"
                  accept=".csv,.tsv,.xlsx,.xls"
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
                className={`min-h-[44px] cursor-pointer self-start rounded-control border border-subtle bg-accent-primary px-5 py-2.5 font-bold text-on-accent transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
              >
                {uploading ? '提交中…' : '开始运行'}
              </button>
            </form>
          </div>
        )}
      </div>

      <div className="flex flex-col gap-2">
        <h2 className="font-bold text-ink">历史跑批</h2>
        {runs.length === 0 && (
          <p className="text-ink-soft">还没有任何跑批记录。在上方上传数据文件开始第一次运行。</p>
        )}
        {runs.length > 0 && (
          <div className="overflow-x-auto overflow-y-hidden rounded-card border border-subtle bg-card">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-subtle bg-paper text-ink">
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
                    className={`cursor-pointer border-b border-subtle text-ink last:border-b-0 hover:bg-paper ${
                      selectedRunId === run.run_id ? 'bg-paper' : ''
                    }`}
                  >
                    <td className="px-3 py-2 font-mono text-xs">{run.run_id}</td>
                    <td className="px-3 py-2">
                      <TaskStatusBadge {...etlRunStatusBadge(run.status)} />
                    </td>
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
        <div className="flex flex-col gap-3 rounded-panel border border-subtle bg-card p-4">
          <h2 className="font-bold text-ink">跑批详情：{selectedRun.run_id}</h2>
          {selectedRun.status === 'failed' && (
            <p role="alert" className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink">
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
              <div className="overflow-x-auto overflow-y-hidden rounded-card border border-subtle">
                <table className="w-full text-left text-sm">
                  <thead>
                    <tr className="border-b border-subtle bg-paper text-ink">
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
                      <tr key={label} className="border-b border-subtle text-ink last:border-b-0">
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
              {selectedRun.report.skipped_mappings && selectedRun.report.skipped_mappings.length > 0 && (
                <div className="flex flex-col gap-2">
                  <h3 className="font-bold text-ink">
                    映射级跳过（共 {selectedRun.report.skipped_mappings.length} 条）
                  </h3>
                  <div className="overflow-x-auto overflow-y-hidden rounded-card border border-subtle">
                    <table className="w-full text-left text-sm">
                      <thead>
                        <tr className="border-b border-subtle bg-paper text-ink">
                          <th className="px-3 py-2">类型</th>
                          <th className="px-3 py-2">文件</th>
                          <th className="px-3 py-2">原因</th>
                        </tr>
                      </thead>
                      <tbody>
                        {selectedRun.report.skipped_mappings.map((mapping, idx) => (
                          <tr key={idx} className="border-b border-subtle text-ink last:border-b-0">
                            <td className="px-3 py-2">{mapping.label}</td>
                            <td className="px-3 py-2">{mapping.source_file}</td>
                            <td className="px-3 py-2">{mapping.reason}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
              {selectedRun.report.skipped_rows && selectedRun.report.skipped_rows.length > 0 && (
                <div className="flex flex-col gap-2">
                  <h3 className="font-bold text-ink">
                    跳过明细（预览前 {SKIPPED_ROWS_PREVIEW_LIMIT} 条，共{' '}
                    {selectedRun.report.skipped_rows.length} 条）
                  </h3>
                  <div className="overflow-x-auto overflow-y-hidden rounded-card border border-subtle">
                    <table className="w-full text-left text-sm">
                      <thead>
                        <tr className="border-b border-subtle bg-paper text-ink">
                          <th className="px-3 py-2">类型</th>
                          <th className="px-3 py-2">文件</th>
                          <th className="px-3 py-2">行号</th>
                          <th className="px-3 py-2">原因</th>
                        </tr>
                      </thead>
                      <tbody>
                        {selectedRun.report.skipped_rows
                          .slice(0, SKIPPED_ROWS_PREVIEW_LIMIT)
                          .map((row, idx) => (
                            <tr key={idx} className="border-b border-subtle text-ink last:border-b-0">
                              <td className="px-3 py-2">{row.label}</td>
                              <td className="px-3 py-2">{row.source_file}</td>
                              <td className="px-3 py-2">{row.row_number}</td>
                              <td className="px-3 py-2">{row.reason}</td>
                            </tr>
                          ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
              {downloadError && (
                <p role="alert" className="text-sm text-ink">
                  {downloadError}
                </p>
              )}
              <button
                type="button"
                onClick={handleDownloadReport}
                disabled={downloadingReport}
                className={`self-start text-sm font-bold text-ink underline decoration-2 underline-offset-2 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
              >
                {downloadingReport ? '下载中…' : '下载完整报告 CSV'}
              </button>
            </>
          )}
        </div>
      )}
    </div>
  )
}
