import { useCallback, useEffect, useState } from 'react'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'
import { useToast } from './ToastContext'
import { useConfirm } from './ConfirmContext'
import { Pager } from './Pager'
import { Skeleton } from './Skeleton'
import { usePaginatedAdminList } from './usePaginatedAdminList'

const PAGE_SIZE = 20

interface DuplicateSuggestion {
  review_id: number
  candidate_a_node_key: string
  candidate_b_node_key: string
  similarity_score: number
  reason: string
}

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

export function DuplicateTermSuggestionsTab() {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const showToast = useToast()
  const confirm = useConfirm()
  const [processingId, setProcessingId] = useState<number | null>(null)

  const fetchPage = useCallback(
    async (page: number) => {
      if (!sessionToken) return { items: [], total: 0 }
      const response = await adminFetch(
        `/api/admin/duplicate-reviews?tenant_id=${encodeURIComponent(tenantId)}&page=${page}&page_size=${PAGE_SIZE}`,
        sessionToken,
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '加载疑似重复列表失败'))
      }
      const data: { suggestions: DuplicateSuggestion[]; total: number } = await response.json()
      return { items: data.suggestions, total: data.total }
    },
    [sessionToken, tenantId],
  )
  const {
    items: suggestions, total, loaded, error, setError, page, setPage, refresh,
  } = usePaginatedAdminList(fetchPage)

  useEffect(() => {
    setPage(1)
  }, [tenantId, setPage])

  const handleApprove = async (reviewId: number, keepNodeKey: string, mergeNodeKey: string) => {
    if (!sessionToken || processingId !== null) return
    const confirmed = await confirm({
      message: `${mergeNodeKey} 的标准名和别名将并入 ${keepNodeKey}，${mergeNodeKey} 将被标记为已合并，此操作不可撤销。`,
      confirmLabel: '合并',
    })
    if (!confirmed) return
    setError(null)
    setProcessingId(reviewId)
    try {
      const response = await adminFetch(`/api/admin/duplicate-reviews/${reviewId}/approve`, sessionToken, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tenant_id: tenantId, keep_node_key: keepNodeKey }),
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '合并失败'))
      }
      showToast('已合并')
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : '合并失败')
    } finally {
      setProcessingId(null)
    }
  }

  const handleReject = async (reviewId: number) => {
    if (!sessionToken || processingId !== null) return
    setError(null)
    setProcessingId(reviewId)
    try {
      const response = await adminFetch(`/api/admin/duplicate-reviews/${reviewId}/reject`, sessionToken, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tenant_id: tenantId, note: null }),
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '驳回失败'))
      }
      showToast('已驳回')
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : '驳回失败')
    } finally {
      setProcessingId(null)
    }
  }

  if (!loaded) {
    return <Skeleton variant="card-list" count={3} />
  }

  return (
    <div className="flex flex-col gap-4">
      {error && (
        <p
          role="alert"
          className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink shadow-soft-sm"
        >
          {error}
        </p>
      )}
      {suggestions.length === 0 ? (
        <p className="text-sm text-ink-soft">暂无疑似重复的术语。</p>
      ) : (
        <ul className="flex flex-col gap-3">
          {suggestions.map((s) => (
            <li
              key={s.review_id}
              className="flex flex-col gap-2 rounded-card border border-subtle bg-card p-4 shadow-soft-sm"
            >
              <p className="text-sm text-ink">
                <span className="font-bold">{s.candidate_a_node_key}</span>
                {' <-> '}
                <span className="font-bold">{s.candidate_b_node_key}</span>
                {`（相似度 ${s.similarity_score.toFixed(2)}）`}
              </p>
              <p className="text-xs text-ink-soft">{s.reason}</p>
              <div className="flex gap-2">
                <button
                  type="button"
                  disabled={processingId !== null}
                  onClick={() => handleApprove(s.review_id, s.candidate_a_node_key, s.candidate_b_node_key)}
                  className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-3 py-1 text-sm ${focusRing} disabled:cursor-not-allowed disabled:opacity-50`}
                >
                  合并（保留 {s.candidate_a_node_key}）
                </button>
                <button
                  type="button"
                  disabled={processingId !== null}
                  onClick={() => handleApprove(s.review_id, s.candidate_b_node_key, s.candidate_a_node_key)}
                  className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-3 py-1 text-sm ${focusRing} disabled:cursor-not-allowed disabled:opacity-50`}
                >
                  合并（保留 {s.candidate_b_node_key}）
                </button>
                <button
                  type="button"
                  disabled={processingId !== null}
                  onClick={() => handleReject(s.review_id)}
                  className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-3 py-1 text-sm ${focusRing} disabled:cursor-not-allowed disabled:opacity-50`}
                >
                  驳回（不是同一个实体）
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
      <Pager page={page} totalPages={Math.max(1, Math.ceil(total / PAGE_SIZE))} onPageChange={setPage} />
    </div>
  )
}
