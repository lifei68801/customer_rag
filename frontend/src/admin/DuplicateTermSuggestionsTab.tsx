import { useCallback, useEffect, useRef, useState } from 'react'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'
import { useToast } from './ToastContext'
import { Pager } from './Pager'
import { Skeleton } from './Skeleton'

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
  const [suggestions, setSuggestions] = useState<DuplicateSuggestion[]>([])
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [processingId, setProcessingId] = useState<number | null>(null)

  // 快速连续翻页会同时有多个请求在途；每次发起请求前递增序号，响应回来时
  // 只有序号仍是"最新"的那一个才允许写入 state——旧请求的响应即使后到，
  // 也不会覆盖新请求已经写入的数据。跟 GraphReviewsPage.tsx 的
  // refreshPending/refreshHistory 用的是同一个模式。
  const requestIdRef = useRef(0)

  const refresh = useCallback(async () => {
    if (!sessionToken) return
    const requestId = ++requestIdRef.current
    try {
      const response = await adminFetch(
        `/api/admin/duplicate-reviews?tenant_id=${encodeURIComponent(tenantId)}&page=${page}&page_size=${PAGE_SIZE}`,
        sessionToken,
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '加载疑似重复列表失败'))
      }
      const data: { suggestions: DuplicateSuggestion[]; total: number } = await response.json()
      if (requestId !== requestIdRef.current) return
      setSuggestions(data.suggestions)
      setTotal(data.total)
    } catch (err) {
      if (requestId !== requestIdRef.current) return
      setError(err instanceof Error ? err.message : '加载疑似重复列表失败')
    } finally {
      if (requestId === requestIdRef.current) {
        setLoaded(true)
      }
    }
  }, [sessionToken, tenantId, page])

  useEffect(() => {
    setPage(1)
  }, [tenantId])

  useEffect(() => {
    refresh()
  }, [refresh])

  const handleApprove = async (reviewId: number, keepNodeKey: string) => {
    if (!sessionToken || processingId !== null) return
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
                  onClick={() => handleApprove(s.review_id, s.candidate_a_node_key)}
                  className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-3 py-1 text-sm ${focusRing} disabled:cursor-not-allowed disabled:opacity-50`}
                >
                  合并（保留 {s.candidate_a_node_key}）
                </button>
                <button
                  type="button"
                  disabled={processingId !== null}
                  onClick={() => handleApprove(s.review_id, s.candidate_b_node_key)}
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
