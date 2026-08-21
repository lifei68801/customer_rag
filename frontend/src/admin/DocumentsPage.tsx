import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useConfirm } from './ConfirmContext'
import { useAdminDensity } from './DensityContext'
import { Skeleton } from './Skeleton'
import { useAdminTenant } from './TenantContext'
import { TaskStatusBadge } from './TaskStatusBadge'
import { useToast } from './ToastContext'
import { Pager } from './Pager'

const PAGE_SIZE = 20

interface TrackedDocument {
  file_path: string
  content_hash: string
  chunk_count: number
  last_ingested_at: string
}

/** file_path 是服务端落盘的完整路径（含 uuid 前缀），管理页面只需要给人看的文件名。 */
function displayFileName(filePath: string): string {
  const segments = filePath.split(/[\\/]/)
  return segments[segments.length - 1] || filePath
}

interface PendingJob {
  job_id: string
  file_path: string
  status: string
  last_error: string | null
  // 后端按 started_at 是否超过卡死阈值现算——只有这里是 true 时才展示
  // 重新执行/删除按钮，避免误伤正常排队/正在处理中的任务。
  is_stuck: boolean
}

interface DeadJob {
  job_id: string
  file_path: string
  last_error: string | null
}

type ChunkPreview = { chunks: string[]; total: number }

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

export function DocumentsPage() {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const confirm = useConfirm()
  const showToast = useToast()
  const { density } = useAdminDensity()
  const [documents, setDocuments] = useState<TrackedDocument[]>([])
  const [pendingJobs, setPendingJobs] = useState<PendingJob[]>([])
  const [deadJobs, setDeadJobs] = useState<DeadJob[]>([])
  const [page, setPage] = useState(1)
  const [documentsTotal, setDocumentsTotal] = useState(0)
  const [jobActionId, setJobActionId] = useState<string | null>(null)
  const [jobError, setJobError] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [buildGraph, setBuildGraph] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [deletingPath, setDeletingPath] = useState<string | null>(null)
  const [expandedChunks, setExpandedChunks] = useState<
    Record<string, ChunkPreview | 'loading'>
  >({})
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [downloadingPath, setDownloadingPath] = useState<string | null>(null)
  // 轮询期间才需要知道"上一次拿到的 pending_jobs 是不是空的"，不需要触发重渲染。
  const hasPendingJobsRef = useRef(false)
  // 让 handleUpload/handleDelete 能在动作完成后立即"踢"一次轮询循环，
  // 不用等已经排好队的 setTimeout 走完最坏 15 秒才发现任务状态变了。
  const pollNowRef = useRef<() => Promise<void>>(async () => {})
  // 快速连续翻页会同时有多个请求在途；每次发起请求前递增请求序号，响应回来
  // 时只有序号仍是"最新"的那一个才允许写入 state——旧请求的响应即使后到，
  // 也不会覆盖新请求已经写入的数据。（照抄 GraphReviewsPage.tsx 的模式。）
  const refreshRequestIdRef = useRef(0)

  useEffect(() => {
    document.title = '文档管理 · 管理后台'
  }, [])

  const refresh = useCallback(async () => {
    if (!sessionToken) return
    const requestId = ++refreshRequestIdRef.current
    const response = await adminFetch(
      `/api/admin/documents?tenant_id=${encodeURIComponent(tenantId)}&page=${page}&page_size=${PAGE_SIZE}`,
      sessionToken,
    )
    const data = (await response.json()) as {
      documents: TrackedDocument[]
      total: number
      pending_jobs: PendingJob[]
      dead_jobs: DeadJob[]
    }
    if (requestId !== refreshRequestIdRef.current) return
    setDocuments(data.documents)
    setDocumentsTotal(data.total)
    setPendingJobs(data.pending_jobs)
    setDeadJobs(data.dead_jobs)
    hasPendingJobsRef.current = data.pending_jobs.length > 0
    setLoaded(true)
  }, [sessionToken, tenantId, page])

  useEffect(() => {
    setPage(1)
  }, [tenantId])

  useEffect(() => {
    if (loaded && documents.length === 0 && page > 1) {
      setPage((p) => p - 1)
    }
  }, [loaded, documents.length, page])

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null

    const poll = async () => {
      if (timer) clearTimeout(timer)
      try {
        await refresh()
      } catch (err) {
        console.error('文档列表刷新失败', err)
      }
      if (cancelled) return
      // 没有处理中任务时没必要每 3 秒打一次后端——退避到 15 秒；上传/删除
      // 完成后会通过 pollNowRef 主动跳过这次等待，不是真的要等满 15 秒。
      const interval = hasPendingJobsRef.current ? 3000 : 15000
      timer = setTimeout(poll, interval)
    }
    pollNowRef.current = poll
    poll()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [refresh])

  const handleUpload = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!sessionToken) return
    const form = event.currentTarget
    const fileInput = form.elements.namedItem('file') as HTMLInputElement
    const file = fileInput.files?.[0]
    if (!file) return

    setUploading(true)
    setUploadError(null)
    try {
      const formData = new FormData()
      formData.append('file', file)
      formData.append('tenant_id', tenantId)
      formData.append('build_graph', String(buildGraph))
      const response = await adminFetch('/api/admin/documents', sessionToken, {
        method: 'POST',
        body: formData,
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '上传失败'))
      }
      showToast('已提交上传')
      form.reset()
      // 复选框是 React 受控组件，form.reset() 只重置原生 file input，
      // 不重置这个状态——不手动清掉的话下一次上传会在不知情的情况下
      // 继续带着"构建图谱"参数提交。
      setBuildGraph(false)
      await pollNowRef.current()
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : '上传失败')
    } finally {
      setUploading(false)
    }
  }

  const handleDelete = async (filePath: string) => {
    if (!sessionToken || deletingPath !== null) return
    if (!(await confirm(`确定要删除「${displayFileName(filePath)}」吗？此操作不可撤销。`))) {
      return
    }
    setDeleteError(null)
    setDeletingPath(filePath)
    try {
      const response = await adminFetch(
        `/api/admin/documents?tenant_id=${encodeURIComponent(tenantId)}&file_path=${encodeURIComponent(filePath)}`,
        sessionToken,
        { method: 'DELETE' },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '删除失败'))
      }
      showToast('已删除文档')
      await pollNowRef.current()
    } catch (err) {
      setDeleteError(err instanceof Error ? err.message : '删除失败')
    } finally {
      setDeletingPath(null)
    }
  }

  const handleRetryJob = async (jobId: string) => {
    if (!sessionToken || jobActionId !== null) return
    setJobError(null)
    setJobActionId(jobId)
    try {
      const response = await adminFetch(
        `/api/admin/documents/jobs/${jobId}/retry?tenant_id=${encodeURIComponent(tenantId)}`,
        sessionToken,
        { method: 'POST' },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '重试失败'))
      }
      showToast('已重新提交')
      await pollNowRef.current()
    } catch (err) {
      setJobError(err instanceof Error ? err.message : '重试失败')
    } finally {
      setJobActionId(null)
    }
  }

  const handleDeleteJob = async (jobId: string, filePath: string) => {
    if (!sessionToken || jobActionId !== null) return
    if (
      !(await confirm(
        `确定要删除任务「${displayFileName(filePath)}」吗？关联的上传文件也会被清理，此操作不可撤销。`,
      ))
    ) {
      return
    }
    setJobError(null)
    setJobActionId(jobId)
    try {
      const response = await adminFetch(
        `/api/admin/documents/jobs/${jobId}?tenant_id=${encodeURIComponent(tenantId)}`,
        sessionToken,
        { method: 'DELETE' },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '删除失败'))
      }
      showToast('已删除任务')
      await pollNowRef.current()
    } catch (err) {
      setJobError(err instanceof Error ? err.message : '删除失败')
    } finally {
      setJobActionId(null)
    }
  }

  const handleTogglePreview = async (filePath: string) => {
    if (!sessionToken) return
    const current = expandedChunks[filePath]
    if (current === 'loading') return
    if (current) {
      setExpandedChunks((prev) => {
        const next = { ...prev }
        delete next[filePath]
        return next
      })
      return
    }
    setPreviewError(null)
    setExpandedChunks((prev) => ({ ...prev, [filePath]: 'loading' }))
    try {
      const response = await adminFetch(
        `/api/admin/documents/chunks?tenant_id=${encodeURIComponent(tenantId)}&file_path=${encodeURIComponent(filePath)}`,
        sessionToken,
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '加载预览失败'))
      }
      const data = (await response.json()) as { chunks: { text: string }[]; total: number }
      setExpandedChunks((prev) => ({
        ...prev,
        [filePath]: { chunks: data.chunks.map((c) => c.text), total: data.total },
      }))
    } catch (err) {
      setPreviewError(err instanceof Error ? err.message : '加载预览失败')
      setExpandedChunks((prev) => {
        const next = { ...prev }
        delete next[filePath]
        return next
      })
    }
  }

  const handleDownloadFile = async (filePath: string) => {
    if (!sessionToken || downloadingPath !== null) return
    setPreviewError(null)
    setDownloadingPath(filePath)
    try {
      const response = await adminFetch(
        `/api/admin/documents/file?tenant_id=${encodeURIComponent(tenantId)}&file_path=${encodeURIComponent(filePath)}`,
        sessionToken,
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '查看原文件失败'))
      }
      const blob = await response.blob()
      const url = URL.createObjectURL(blob)
      window.open(url, '_blank')
      // 新标签页/浏览器内置查看器（比如 PDF）此时可能还没读完这个
      // blob URL 的内容，不能在 window.open 后立即 revoke；60 秒后统一
      // 释放，足够覆盖打开+渲染的时间。
      setTimeout(() => URL.revokeObjectURL(url), 60_000)
    } catch (err) {
      setPreviewError(err instanceof Error ? err.message : '查看原文件失败')
    } finally {
      setDownloadingPath(null)
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-xl font-bold text-ink">文档管理（租户：{tenantId}）</h1>

      <form
        onSubmit={handleUpload}
        className="flex flex-col gap-3 rounded-panel border border-subtle bg-card p-4 shadow-soft"
      >
        <input type="file" name="file" required className="text-ink" />
        <label className="flex items-center gap-2 text-sm text-ink">
          <input
            type="checkbox"
            checked={buildGraph}
            onChange={(event) => setBuildGraph(event.target.checked)}
          />
          同时构建知识图谱（LLM 关系抽取，耗时更久）
        </label>
        {uploadError && (
          <p role="alert" className="text-sm text-ink">
            {uploadError}
          </p>
        )}
        <button
          type="submit"
          disabled={uploading}
          className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-accent-pink px-5 py-2.5 font-bold text-ink shadow-soft transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
        >
          {uploading ? '上传中…' : '上传文档'}
        </button>
      </form>

      {jobError && (
        <p
          role="alert"
          className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink shadow-soft-sm"
        >
          {jobError}
        </p>
      )}

      {pendingJobs.length > 0 && (
        <div className="flex flex-col gap-2">
          <h2 className="font-bold text-ink">处理中的任务</h2>
          {pendingJobs.map((job) =>
            job.is_stuck ? (
              <div
                key={job.job_id}
                className="flex items-center justify-between rounded-card border border-status-error bg-card px-4 py-3 shadow-soft-sm"
              >
                <span className="text-ink">
                  {displayFileName(job.file_path)}
                  <span className="text-ink-soft">
                    {' '}
                    （疑似卡死：超过 30 分钟没有任何进展，很可能是服务重启中断了处理、或者一直没有等到下一次批处理触发，可以手动重新执行或删除）
                  </span>
                </span>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => handleRetryJob(job.job_id)}
                    disabled={jobActionId !== null}
                    className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-accent-pink px-3 py-1.5 text-sm font-bold text-ink shadow-soft-sm transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                  >
                    {jobActionId === job.job_id ? '处理中…' : '重新执行'}
                  </button>
                  <button
                    type="button"
                    onClick={() => handleDeleteJob(job.job_id, job.file_path)}
                    disabled={jobActionId !== null}
                    className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-3 py-1.5 text-sm font-bold text-ink shadow-soft-sm transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                  >
                    {jobActionId === job.job_id ? '处理中…' : '删除'}
                  </button>
                </div>
              </div>
            ) : (
              <div
                key={job.job_id}
                className={`rounded-card border bg-accent-yellow px-3 py-2 text-sm text-ink shadow-soft-sm ${
                  job.last_error ? 'border-status-error' : 'border-ink'
                }`}
              >
                {displayFileName(job.file_path)}{' '}
                <TaskStatusBadge tone="active" label="处理中" />
                {job.last_error && <span className="text-ink"> (错误：{job.last_error})</span>}
              </div>
            ),
          )}
        </div>
      )}

      {deadJobs.length > 0 && (
        <div className="flex flex-col gap-2">
          <h2 className="font-bold text-ink">失败任务</h2>
          {deadJobs.map((job) => (
            <div
              key={job.job_id}
              className="flex items-center justify-between rounded-card border border-status-error bg-card px-4 py-3 shadow-soft-sm"
            >
              <span className="text-ink">
                {displayFileName(job.file_path)}
                {job.last_error && `（${job.last_error}）`}
              </span>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => handleRetryJob(job.job_id)}
                  disabled={jobActionId !== null}
                  className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-accent-pink px-3 py-1.5 text-sm font-bold text-ink shadow-soft-sm transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                >
                  {jobActionId === job.job_id ? '处理中…' : '重试'}
                </button>
                <button
                  type="button"
                  onClick={() => handleDeleteJob(job.job_id, job.file_path)}
                  disabled={jobActionId !== null}
                  className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-3 py-1.5 text-sm font-bold text-ink shadow-soft-sm transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                >
                  {jobActionId === job.job_id ? '处理中…' : '删除'}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="flex flex-col gap-2">
        <h2 className="font-bold text-ink">已摄取文档</h2>
        {deleteError && (
          <p
            role="alert"
            className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink shadow-soft-sm"
          >
            {deleteError}
          </p>
        )}
        {!loaded && <Skeleton variant="card-list" count={3} />}
        {previewError && (
          <p
            role="alert"
            className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink shadow-soft-sm"
          >
            {previewError}
          </p>
        )}
        {loaded &&
          documents.map((doc) => {
            const preview = expandedChunks[doc.file_path]
            return (
              <div
                key={doc.file_path}
                className={`flex flex-col gap-2 rounded-card border border-subtle bg-card shadow-soft-sm ${
                  density === 'compact' ? 'px-2.5 py-1.5' : 'px-4 py-3'
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className="text-ink" title={doc.file_path}>
                    {displayFileName(doc.file_path)}（{doc.chunk_count} chunks，最近摄取：
                    {doc.last_ingested_at}）
                  </span>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => handleTogglePreview(doc.file_path)}
                      disabled={preview === 'loading'}
                      className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-3 py-1.5 text-sm font-bold text-ink shadow-soft-sm transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                    >
                      {preview === 'loading'
                        ? '加载中…'
                        : preview
                          ? '收起预览'
                          : '预览'}
                    </button>
                    <button
                      type="button"
                      onClick={() => handleDownloadFile(doc.file_path)}
                      disabled={downloadingPath !== null}
                      className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-3 py-1.5 text-sm font-bold text-ink shadow-soft-sm transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                    >
                      {downloadingPath === doc.file_path ? '打开中…' : '查看原文件'}
                    </button>
                    <button
                      type="button"
                      onClick={() => handleDelete(doc.file_path)}
                      disabled={deletingPath !== null}
                      className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-3 py-1.5 text-sm font-bold text-ink shadow-soft-sm transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                    >
                      {deletingPath === doc.file_path ? '删除中…' : '删除'}
                    </button>
                  </div>
                </div>
                {preview && preview !== 'loading' && (
                  <div className="flex flex-col gap-1 border-t-2 border-ink pt-2">
                    {preview.chunks.map((text, i) => (
                      <p
                        key={i}
                        className="rounded-card border border-subtle bg-paper px-2 py-1 text-xs text-ink-soft"
                      >
                        [{i}] {text}
                      </p>
                    ))}
                    {preview.total > preview.chunks.length && (
                      <p className="text-xs text-ink-soft">
                        仅显示前 {preview.chunks.length} / 共 {preview.total} 条
                      </p>
                    )}
                    {preview.chunks.length === 0 && (
                      <p className="text-xs text-ink-soft">向量库里没有找到这份文档的 chunk。</p>
                    )}
                  </div>
                )}
              </div>
            )
          })}
        {loaded && documents.length === 0 && (
          <p className="text-ink-soft">
            当前租户还没有已摄取的文档。去
            <Link to="/admin/data-entry" className="font-bold underline">
              数据加工
            </Link>
            上传一份试试。
          </p>
        )}
        {loaded && documents.length > 0 && (
          <Pager
            page={page}
            totalPages={Math.max(1, Math.ceil(documentsTotal / PAGE_SIZE))}
            onPageChange={setPage}
          />
        )}
      </div>
    </div>
  )
}
