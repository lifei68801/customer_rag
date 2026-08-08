import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { adminFetch } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './useAdminTenant'

interface TrackedDocument {
  file_path: string
  content_hash: string
  chunk_count: number
}

interface PendingJob {
  job_id: string
  file_path: string
  status: string
  last_error: string | null
}

export function DocumentsPage() {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const [documents, setDocuments] = useState<TrackedDocument[]>([])
  const [pendingJobs, setPendingJobs] = useState<PendingJob[]>([])
  const [buildGraph, setBuildGraph] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    if (!sessionToken) return
    const response = await adminFetch(
      `/admin/documents?tenant_id=${encodeURIComponent(tenantId)}`,
      sessionToken,
    )
    const data = (await response.json()) as {
      documents: TrackedDocument[]
      pending_jobs: PendingJob[]
    }
    setDocuments(data.documents)
    setPendingJobs(data.pending_jobs)
  }, [sessionToken, tenantId])

  useEffect(() => {
    const poll = () => {
      refresh().catch((err) => {
        console.error('文档列表刷新失败', err)
      })
    }
    poll()
    const timer = setInterval(poll, 3000)
    return () => clearInterval(timer)
  }, [refresh])

  const handleUpload = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!sessionToken) return
    const form = event.currentTarget
    const fileInput = form.elements.namedItem('file') as HTMLInputElement
    const file = fileInput.files?.[0]
    if (!file) return

    setUploading(true)
    setError(null)
    try {
      const formData = new FormData()
      formData.append('file', file)
      formData.append('tenant_id', tenantId)
      formData.append('build_graph', String(buildGraph))
      const response = await adminFetch('/admin/documents', sessionToken, {
        method: 'POST',
        body: formData,
      })
      if (!response.ok) {
        const body = (await response.json()) as { detail?: string }
        throw new Error(body.detail ?? '上传失败')
      }
      form.reset()
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : '上传失败')
    } finally {
      setUploading(false)
    }
  }

  const handleDelete = async (filePath: string) => {
    if (!sessionToken) return
    setError(null)
    try {
      const response = await adminFetch(
        `/admin/documents?tenant_id=${encodeURIComponent(tenantId)}&file_path=${encodeURIComponent(filePath)}`,
        sessionToken,
        { method: 'DELETE' },
      )
      if (!response.ok) {
        const body = (await response.json().catch(() => ({}))) as { detail?: string }
        throw new Error(body.detail ?? '删除失败')
      }
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除失败')
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-xl font-bold text-ink">文档管理（租户：{tenantId}）</h1>

      <form
        onSubmit={handleUpload}
        className="flex flex-col gap-3 border-2 border-ink bg-card p-4 shadow-brutal"
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
        {error && <p className="text-sm text-ink">{error}</p>}
        <button
          type="submit"
          disabled={uploading}
          className="min-h-[44px] cursor-pointer border-2 border-ink bg-accent-pink px-5 py-2.5 font-bold text-ink shadow-brutal transition active:translate-x-[2px] active:translate-y-[2px] active:shadow-none disabled:cursor-not-allowed disabled:opacity-50"
        >
          {uploading ? '上传中…' : '上传文档'}
        </button>
      </form>

      {pendingJobs.length > 0 && (
        <div className="flex flex-col gap-2">
          <h2 className="font-bold text-ink">处理中的任务</h2>
          {pendingJobs.map((job) => (
            <div
              key={job.job_id}
              className={`border bg-accent-yellow px-3 py-2 text-sm text-ink shadow-brutal-sm ${
                job.last_error ? 'border-status-error' : 'border-ink'
              }`}
            >
              {job.file_path} — {job.status}
              {job.last_error && <span className="text-ink"> (错误：{job.last_error})</span>}
            </div>
          ))}
        </div>
      )}

      <div className="flex flex-col gap-2">
        <h2 className="font-bold text-ink">已摄取文档</h2>
        {documents.map((doc) => (
          <div
            key={doc.file_path}
            className="flex items-center justify-between border-2 border-ink bg-card px-4 py-3 shadow-brutal-sm"
          >
            <span className="text-ink">
              {doc.file_path}（{doc.chunk_count} chunks）
            </span>
            <button
              type="button"
              onClick={() => handleDelete(doc.file_path)}
              className="min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-3 py-1.5 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none"
            >
              删除
            </button>
          </div>
        ))}
        {documents.length === 0 && <p className="text-ink-soft">当前租户还没有已摄取的文档。</p>}
      </div>
    </div>
  )
}
